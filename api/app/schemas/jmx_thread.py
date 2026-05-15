"""3 种线程组 VO，对齐 Java ThreadGroupVO / SteppingThreadGroupVO / ConcurrencyThreadGroupVO。"""

from __future__ import annotations

from app.schemas.base import BaseVO, CamelModel


class ThreadGroupVO(BaseVO):
    test_case_id: int | None = None
    jmx_id: int | None = None
    num_threads: str | None = None
    ramp_time: str | None = None
    loops: str | None = None
    same_user_on_next_iteration: int | None = None
    delayed_start: int | None = None
    scheduler: int | None = None
    duration: str | None = None
    delay: str | None = None


class SteppingThreadGroupVO(BaseVO):
    test_case_id: int | None = None
    jmx_id: int | None = None
    num_threads: str | None = None
    first_wait_for_seconds: str | None = None
    then_start_threads: str | None = None
    next_add_threads: str | None = None
    next_add_threads_every_seconds: str | None = None
    using_ramp_up_seconds: str | None = None
    then_hold_load_for_seconds: str | None = None
    finally_stop_threads: str | None = None
    finally_stop_threads_every_seconds: str | None = None


class ConcurrencyThreadGroupVO(BaseVO):
    test_case_id: int | None = None
    jmx_id: int | None = None
    target_concurrency: str | None = None
    ramp_up_time: str | None = None
    ramp_up_steps_count: str | None = None
    hold_target_rate_time: str | None = None
