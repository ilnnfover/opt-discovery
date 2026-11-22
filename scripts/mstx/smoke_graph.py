"""两轮 Decode Graph 冒烟：验证 MSTX 打点在 vLLM worker 内落库。

由 ``scripts/enable_mstx.sh`` 启动（它负责注入 ``PYTHONPATH=scripts/mstx`` 与
``VLLM_MSTX_PATCH=1``，并把导出目录写进 ``MSTX_TRACE_DIR``）：

    bash scripts/enable_mstx.sh /tmp/mstx-smoke -- python3 scripts/mstx/smoke_graph.py

前置：``MODEL_PATH`` 指向本地模型；TP=2 需要两张空闲卡。建议同时设置
``VLLM_WORKER_MULTIPROC_METHOD=spawn``（fork 会让 EngineCore 继承失效的
OpenMP 线程池，见 docs/engineering/profiler-capture-followup.md）。

轮数由 ``MSTX_ROUNDS`` 控制（默认 2）。``stop_profile()`` 触发的在线导出常被
RPC 超时打断，因此这里吞掉异常并留出时间；导出不完整时用
``scripts/mstx/export_offline.sh`` 对 raw 目录补导。
"""

from __future__ import annotations

import os
import time


def main() -> int:
    from vllm import LLM, SamplingParams

    trace_dir = os.environ.get("MSTX_TRACE_DIR")
    if not trace_dir:
        print("需要 MSTX_TRACE_DIR（由 scripts/enable_mstx.sh 注入）", flush=True)
        return 2
    rounds = int(os.environ.get("MSTX_ROUNDS", "2"))

    t0 = time.time()
    print("PROCESS_START_ABS", t0, flush=True)
    llm = LLM(
        model=os.environ["MODEL_PATH"],
        tensor_parallel_size=2,
        dtype="bfloat16",
        max_model_len=8192,
        max_num_seqs=32,
        gpu_memory_utilization=0.85,
        enforce_eager=False,
        compilation_config={
            "cudagraph_mode": "FULL_AND_PIECEWISE",
            "cudagraph_capture_sizes": [16, 32, 1024],
        },
        enable_prefix_caching=False,
        enable_chunked_prefill=True,
        seed=42,
        trust_remote_code=False,
        additional_config={"ascend_compilation_config": {"enable_npugraph_ex": False}},
        profiler_config={"profiler": "torch", "torch_profiler_dir": trace_dir},
    )
    tokenizer = llm.get_tokenizer()
    ids = tokenizer.encode("Hi")[:1]
    print("LOAD_SECONDS", round(time.time() - t0, 1), flush=True)

    sampling = SamplingParams(temperature=0.0, max_tokens=64)
    llm.generate([{"prompt_token_ids": list(ids)}], sampling)  # warmup，不进窗口
    print("WARMUP_DONE_ABS", time.time(), flush=True)

    llm.start_profile()
    print("PROFILE_START_ABS", time.time(), flush=True)
    for index in range(rounds):
        start = time.time()
        outputs = llm.generate([{"prompt_token_ids": list(ids)}], sampling)
        print(
            "ACTIVE_ROUND", index, "start", start, "end", time.time(),
            "out_tokens", len(outputs[0].outputs[0].token_ids), flush=True,
        )
    print("PROFILE_STOP_ABS", time.time(), flush=True)

    try:
        llm.stop_profile()
        print("PROFILE_STOPPED_ABS", time.time(), flush=True)
    except Exception as exc:  # noqa: BLE001 - RPC 超时后引擎可能已退出
        print("PROFILE_STOP_ERROR", repr(exc), flush=True)
    time.sleep(45)  # 给 worker 侧导出留时间；不完整时走 export_offline.sh
    print("TOTAL_SECONDS", round(time.time() - t0, 1), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
