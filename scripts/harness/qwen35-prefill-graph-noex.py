import os, time
os.environ.setdefault("HF_HOME", "/root/.cache/huggingface")
os.environ.setdefault("XDG_CACHE_HOME", "/root/.cache")
os.environ.pop("VLLM_TARGET_DEVICE", None)
from vllm import LLM, SamplingParams
t0 = time.time()
print("PROCESS_START_ABS", t0, flush=True)
llm = LLM(model=os.environ["MODEL_PATH"], tensor_parallel_size=2, dtype="bfloat16", max_model_len=8192, max_num_seqs=32, gpu_memory_utilization=0.85, enforce_eager=False, compilation_config={"cudagraph_mode": "FULL_AND_PIECEWISE", "cudagraph_capture_sizes": [16, 32, 1024]}, enable_prefix_caching=False, enable_chunked_prefill=True, seed=42, trust_remote_code=False, additional_config={"ascend_compilation_config": {"enable_npugraph_ex": False}})
tok = llm.get_tokenizer()
ids = tok.encode("The capital of France is Paris. " * 300)[:1024]
print("PROMPT_TOKENS", len(ids), "LOAD_SECONDS", round(time.time() - t0, 1), flush=True)
sp = SamplingParams(temperature=0.0, max_tokens=1)
for i in range(5):
    llm.generate([{"prompt_token_ids": list(ids)}], sp)
print("WARMUP_DONE_ABS", time.time(), flush=True)
print("PROFILE_START_ABS", time.time(), flush=True)
for i in range(5):
    ts = time.time()
    outs = llm.generate([{"prompt_token_ids": list(ids)}], sp)
    print("ACTIVE_ROUND", i, "start", ts, "end", time.time(), "sample", repr(outs[0].outputs[0].text[:30]), flush=True)
print("PROFILE_STOP_ABS", time.time(), flush=True)
try:
    import torch, torch_npu  # noqa: F401
    torch.npu.synchronize()
except Exception:
    pass
import time as _mstx_wait  # MSPROF_FLUSH_WAIT
_mstx_wait.sleep(3)
print("TOTAL_SECONDS", round(time.time() - t0, 1), flush=True)
