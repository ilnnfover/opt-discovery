"""在采集进程里补 MSTX 打点（B：host_silent / sync_wait 的证据源）。

用法（放在 PYTHONPATH 里，sitecustomize 会被解释器自动加载）：

    PYTHONPATH=<repo>/scripts/mstx \
    VLLM_MSTX_PATCH=1 python3 $NPU_STACK/qwen35-decode-only.py

打点内容：

| 名字 | 位置 | 用途 |
|---|---|---|
| ``engine_step`` | ``EngineCore.step`` | 一步调度；用来解释 Host 静默空洞落在哪个 Python 阶段 |
| ``execute_model`` | worker 的 ``execute_model`` | 前向执行区间 |
| ``sync_wait_stream`` | ``torch.npu.Stream.synchronize`` | 流同步等待，支撑 ``synchronization_bound`` |
| ``sync_wait_device`` | ``torch.npu.synchronize`` | 设备级同步等待 |

注意：

* 在发出打点的进程内启用 torch_npu.profiler 的 MSTX，并导出 DB。
* 用 ``scripts/enable_mstx.sh`` 启动时会检查实际输出的 ``MSTX_EVENTS``。
"""

import os

_ENABLED = os.environ.get("VLLM_MSTX_PATCH") == "1"


def _install() -> None:
    if not _ENABLED:
        return
    try:
        import torch_npu

        mstx = torch_npu.npu.mstx
    except Exception:  # pragma: no cover - 无 NPU 环境
        return

    def wrap(owner, attribute: str, label: str) -> None:
        original = getattr(owner, attribute, None)
        if original is None or getattr(original, "_mstx_wrapped", False):
            return

        def patched(*args, **kwargs):
            # 本机签名：range_start(message, stream=None, domain='default') -> int
            #           range_end(range_id, domain='default')
            # 只 start 不 end 会产生不闭合区间，因此必须带 id 回传。
            range_id = None
            try:
                range_id = mstx.range_start(label)
            except Exception:
                range_id = None
            try:
                return original(*args, **kwargs)
            finally:
                if range_id is not None:
                    try:
                        mstx.range_end(range_id)
                    except TypeError:
                        try:  # 兼容旧签名 range_end()
                            mstx.range_end()
                        except Exception:
                            pass
                    except Exception:
                        pass

        patched._mstx_wrapped = True  # type: ignore[attr-defined]
        setattr(owner, attribute, patched)

    # 1) 引擎 step：把"静默空洞"落到具体的调度阶段
    try:
        import vllm.v1.engine.core as core

        wrap(core.EngineCore, "step", "engine_step")
    except Exception:
        pass

    # 2) 模型前向
    try:
        import vllm.v1.worker.gpu_model_runner as runner

        wrap(runner.GPUModelRunner, "execute_model", "execute_model")
    except Exception:
        pass

    # 3) 同步等待：这是 synchronization_bound 的实测来源
    try:
        import torch

        wrap(torch.npu.Stream, "synchronize", "sync_wait_stream")
    except Exception:
        pass
    try:
        import torch

        wrap(torch.npu, "synchronize", "sync_wait_device")
    except Exception:
        pass


_install()
