#!/usr/bin/env python3
r"""AttentionStore 变更通知测试：注意力开启/关闭必须产生一条通知。

只测状态机 + notifier 契约，不碰 HTTP。
运行：python3 .skills/qq_adapter/tests/test_attention_notice.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from qq_adapter.attention import (  # noqa: E402
    AttentionStore, render_attention_notice)


def _store(tmp: str, events: list, idle: int = 600) -> AttentionStore:
    def notifier(reason, previous, current):
        events.append((reason, previous, current))
    return AttentionStore(path=str(Path(tmp) / "attention.json"),
                          idle_timeout=idle, notifier=notifier)


def _case(name, fn) -> bool:
    try:
        fn()
        print(f"✓ {name}")
        return True
    except AssertionError as exc:
        print(f"✗ {name}: {exc}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"✗ {name}: 异常 {exc!r}")
        return False


def t_manual_open(tmp):
    events = []
    store = _store(tmp, events)
    store.set("group_1")
    assert events == [("manual", "", "group_1")], events


def t_same_group_repeat(tmp):
    events = []
    store = _store(tmp, events)
    store.set("group_1")
    store.set("group_1")
    assert events == [("manual", "", "group_1")], events


def t_switch(tmp):
    events = []
    store = _store(tmp, events)
    store.set("group_1")
    store.set("group_2")
    assert events[-1] == ("manual", "group_1", "group_2"), events


def t_manual_off(tmp):
    events = []
    store = _store(tmp, events)
    store.set("group_1")
    store.set("")
    assert events[-1] == ("manual", "group_1", ""), events


def t_follow(tmp):
    events = []
    store = _store(tmp, events)
    store.follow("group_7")
    assert events == [("follow", "", "group_7")], events
    store.follow("group_7")
    assert len(events) == 1, events


def t_touch(tmp):
    events = []
    store = _store(tmp, events)
    store.set("group_1")
    store.touch("group_1")
    assert len(events) == 1, events


def t_idle_expire(tmp):
    events = []
    store = _store(tmp, events, idle=0)
    store.set("group_1")
    assert store.get() == ""
    assert events[-1] == ("expire", "group_1", ""), events


def t_notifier_boom(tmp):
    def boom(reason, previous, current):
        raise RuntimeError("network down")
    store = AttentionStore(path=str(Path(tmp) / "attention.json"),
                           notifier=boom)
    store.set("group_1")
    assert store.get() == "group_1"


def t_render():
    text = render_attention_notice("manual", "", "group_200000001")
    assert "group_200000001" in text and "影响" in text
    assert "attention_set" in text and "attention_get" in text
    assert "自动关闭" in render_attention_notice("expire", "group_1", "")


def main() -> int:
    cases = [
        ("手动开启通知", t_manual_open),
        ("重复设同一群不报", t_same_group_repeat),
        ("切换通知带 previous", t_switch),
        ("手动关闭通知", t_manual_off),
        ("自动跟随 reason=follow", t_follow),
        ("touch 不通知", t_touch),
        ("闲置过期通知", t_idle_expire),
        ("notifier 抛异常不影响状态", t_notifier_boom),
        ("通知文本含影响与操作", lambda _t: t_render()),
    ]
    bad = 0
    for name, fn in cases:
        with tempfile.TemporaryDirectory() as tmp:
            if not _case(name, lambda fn=fn, tmp=tmp: fn(tmp)):
                bad += 1
    print(f"\n{len(cases) - bad}/{len(cases)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
