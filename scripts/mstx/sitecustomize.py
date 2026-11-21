"""在采集进程里补 MSTX 打点（B：host_silent / sync_wait 的证据源）。

用法（放在 ``PYTHONPATH`` 最前面，sitecustomize 会被解释器自动加载）：

    PYTHONPATH=<repo>/scripts/mstx:$PYTHONPATH \
    VLLM_MSTX_PATCH=1 python3 <harness>.py

打点内容：

| 名字 | 位置 | 用途 |
|---|---|---|
| ``engine_step`` | ``EngineCore.step`` | 一步调度；用来解释 Host 静默空洞落在哪个 Python 阶段 |
| ``worker_execute`` | ``NPUWorker.execute_model`` | Ascend worker 的前向执行区间（整图/逐算子都覆盖） |
| ``execute_model`` | ``NPUModelRunner`` / ``GPUModelRunner.execute_model`` | 模型前向 |
| ``sync_wait_stream`` | ``torch.npu.Stream.synchronize`` | 流同步等待，支撑 ``synchronization_bound`` |
| ``sync_wait_device`` | ``torch.npu.synchronize`` | 设备级同步等待 |

三个已验证的坑：

1. **``msprof --msproftx=on`` 采不到 PyTorch 的 mstx**（``range_start`` 恒返回 0）。
   必须在进程内启用 ``torch_npu.profiler`` 且 ``experimental_config`` 里
   ``mstx=True``；``vllm_ascend`` 把它写死成 ``msprof_tx=False``，需要外部覆盖。
2. **挂点要挂 Ascend 的子类**：``vllm_ascend`` 用 ``NPUModelRunner`` / ``NPUWorker``
   覆盖了上游实现，只挂 ``GPUModelRunner`` 不会触发（实测静默覆盖率 0%）。
3. **``stop_profile()`` 的在线导出容易因 RPC 超时被 ``cancelled``**。raw 数据本身
   完整，用 ``scripts/mstx/export_offline.sh`` 离线导出即可拿到
   ``msprof_*.db``（含 ``TASK`` / ``CANN_API`` / ``MSTX_EVENTS``）和 CSV。
"""

import os

#: (模块名, 类名, 方法名, 打点标签)。顺序即优先级；同一类被包装一次后不再重复。
TARGETS: tuple[tuple[str, str, str, str], ...] = (
    ("vllm.v1.engine.core", "EngineCore", "step", "engine_step"),
    # vLLM-Ascend 的子类：只挂 GPUModelRunner 不会生效
    ("vllm_ascend.worker.worker", "NPUWorker", "execute_model", "worker_execute"),
    ("vllm_ascend.worker.model_runner_v1", "NPUModelRunner", "execute_model", "execute_model"),
    # 上游 vLLM 路径保留，便于非 Ascend 后端或未来版本
    ("vllm.v1.worker.gpu_model_runner", "GPUModelRunner", "execute_model", "execute_model"),
)

#: 同步等待打点：(owner 表达式, 属性, 标签)
SYNC_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("torch.npu.Stream", "synchronize", "sync_wait_stream"),
    ("torch.npu", "synchronize", "sync_wait_device"),
)


def _wrap(owner, attribute: str, label: str, mstx) -> None:
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


def _install() -> None:
    """按 ``TARGETS`` / ``SYNC_TARGETS`` 包装；任一 import 失败都静默跳过。"""
    if os.environ.get("VLLM_MSTX_PATCH") != "1":
        return
    try:
        import torch_npu

        mstx = torch_npu.npu.mstx
    except Exception:  # pragma: no cover - 无 NPU 环境
        return

    for module_name, class_name, attribute, label in TARGETS:
        try:
            module = __import__(module_name, fromlist=["_"])
            _wrap(getattr(module, class_name), attribute, label, mstx)
        except Exception:
            continue

    for owner_path, attribute, label in SYNC_TARGETS:
        try:
            # 逐段 getattr，避免 `import torch.npu` 这类子模块导入失败
            parts = owner_path.split(".")
            owner = __import__(parts[0])
            for part in parts[1:]:
                owner = getattr(owner, part)
            _wrap(owner, attribute, label, mstx)
        except Exception:
            continue


_install()
