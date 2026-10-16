#!/usr/bin/env python3
"""self_message_watch —— 本账号发出的消息实时留证（账号安全取证用）

背景：2026-10-15 出现「同账号的另一个登录端以我的名义在群里发言」事件。
判定的第一步就是把 NapCat 推来的 message_sent 原文留档，事后与
adapter.log 的发送链（sent group / hook action executed）配对——配对不上
的 self 消息，就不是本 adapter 发的。

用法：
    python3 self_message_watch.py                 # 前台/常驻
    nohup python3 self_message_watch.py &         # 常驻后台
输出：<out>（默认 /home/sandbox/skill_data/qq_adapter/sent_watch.jsonl，jsonl 追加）
每条记录：{"ts": "...", "post_type": "message|message_sent", "raw": {...事件原文...}}

常驻会一直长：文件超过 --max-bytes（默认 5 MB）时自动轮转成 <out>.1（只留一代），
免得把磁盘和检索都拖慢。
"""
import argparse
import asyncio
import json
import os
import pathlib
import time

import websockets

DEFAULT_WS = "ws://host.docker.internal:3001"
DEFAULT_TOKEN_FILE = "/home/sandbox/.secrets/napcat_token.txt"
DEFAULT_OUT = "/home/sandbox/skill_data/qq_adapter/sent_watch.jsonl"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024


def rotate_if_needed(out: pathlib.Path, max_bytes: int):
    """超过上限就滚动成 <out>.1（覆盖上一代），返回是否发生了轮转。"""
    try:
        if max_bytes > 0 and out.exists() and out.stat().st_size >= max_bytes:
            os.replace(out, pathlib.Path(str(out) + ".1"))
            return True
    except OSError:
        pass
    return False


async def run(args):
    token = pathlib.Path(args.token_file).read_text(encoding="utf-8").strip()
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        conn = websockets.connect(args.ws, additional_headers=hdr, open_timeout=10)
    except TypeError:  # 老版本 websockets 用 extra_headers
        conn = websockets.connect(args.ws, extra_headers=hdr, open_timeout=10)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    async with conn as ws:
        rotate_if_needed(out, args.max_bytes)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"watch": "start", "ts": time.strftime("%F %T")},
                               ensure_ascii=False) + "\n")
            f.flush()
            while True:
                raw = await ws.recv()
                try:
                    d = json.loads(raw)
                except Exception:
                    continue
                if d.get("post_type") not in ("message", "message_sent"):
                    continue
                f.write(json.dumps({"ts": time.strftime("%F %T"),
                                    "post_type": d["post_type"], "raw": d},
                                   ensure_ascii=False) + "\n")
                f.flush()


def main():
    ap = argparse.ArgumentParser(description="本账号消息实时留证（账号安全）")
    ap.add_argument("--ws", default=DEFAULT_WS)
    ap.add_argument("--token-file", default=DEFAULT_TOKEN_FILE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES,
                    help="单文件上限，超过则轮转为 <out>.1（0=不轮转）")
    args = ap.parse_args()
    while True:
        try:
            asyncio.run(run(args))
        except KeyboardInterrupt:
            break
        except Exception as e:  # 断线重连
            print("reconnect:", e, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
