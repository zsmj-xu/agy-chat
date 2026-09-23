#!/usr/bin/env python3
"""
双子对话台 — 网页上跟 Claude 或 agy(Gemini) 聊天。零第三方 Web 框架，只用标准库起服务。

两侧接法（混合）：
  - Claude 侧: anthropic SDK，经环境里的 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL 代理，
               模型 global.anthropic.claude-opus-4-8（带兜底）。
  - agy 侧:    `agy --prompt=<...> --model <...>` CLI（复用你的 OAuth 登录），子进程流式。
回复以 HTTP chunked 实时回传浏览器。
"""
import os
import json
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic

HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))

ENV = dict(os.environ)
ENV["PATH"] = os.path.expanduser("~/.local/bin") + os.pathsep + ENV.get("PATH", "")

# Claude 侧模型：按序兜底（代理白名单里存在的）
CLAUDE_MODELS = [
    "global.anthropic.claude-opus-4-8",
    "global.anthropic.claude-opus-4-7",
    "global.anthropic.claude-opus-4-6-v1",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
]

# agy 侧可选模型（也供前端下拉）
AGY_MODELS = [
    "gemini-3.1-pro-high",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-low",
]
DEFAULT_AGY_MODEL = "gemini-3.8-flash-high"

REPLY_TIMEOUT = 240
_client = anthropic.Anthropic()  # 自动读 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL


def build_prompt(history, message):
    """agy CLI 是无状态单轮，把历史+当前问题拼成一个 prompt。"""
    turns = (history or [])[-12:]
    if not turns:
        return message
    lines = ["[对话历史]"]
    for t in turns:
        who = "用户" if t.get("role") == "user" else "助手"
        lines.append(f"{who}：{t.get('content', '')}")
    lines += ["", "[当前问题]", message]
    return "\n".join(lines)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _headers(self, code, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()

    # ---------------- GET ----------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/config":
            body = json.dumps({
                "agyModels": AGY_MODELS,
                "defaultAgyModel": DEFAULT_AGY_MODEL,
            }).encode("utf-8")
            self._headers(200, "application/json; charset=utf-8",
                          {"Content-Length": str(len(body))})
            self.wfile.write(body)
        else:
            self._headers(404, "text/plain", {"Content-Length": "0"})

    def _serve_file(self, name, ctype):
        try:
            with open(os.path.join(HERE, name), "rb") as f:
                data = f.read()
        except OSError:
            self._headers(404, "text/plain", {"Content-Length": "0"})
            return
        self._headers(200, ctype, {"Content-Length": str(len(data))})
        self.wfile.write(data)

    # ---------------- POST ----------------
    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/chat":
            self._headers(404, "text/plain", {"Content-Length": "0"})
            return
        try:
            n = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._headers(400, "text/plain", {"Content-Length": "0"})
            return

        agent = payload.get("agent", "claude")
        model = payload.get("model", DEFAULT_AGY_MODEL)
        message = (payload.get("message") or "").strip()
        history = payload.get("history") or []
        if agent not in ("claude", "agy") or not message:
            self._headers(400, "text/plain", {"Content-Length": "0"})
            return

        self._headers(200, "text/plain; charset=utf-8",
                      {"Transfer-Encoding": "chunked",
                       "Cache-Control": "no-cache",
                       "X-Accel-Buffering": "no"})
        try:
            if agent == "claude":
                self._stream_claude(history, message)
            else:
                self._stream_agy(history, model, message)
        finally:
            self._end_chunks()

    # ---------------- chunked helpers ----------------
    def _chunk(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not data:
            return
        try:
            self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, OSError):
            raise

    def _end_chunks(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    # ---------------- Claude via anthropic SDK ----------------
    def _stream_claude(self, history, message):
        msgs = [{"role": t.get("role"), "content": t.get("content", "")}
                for t in (history or []) if t.get("role") in ("user", "assistant")]
        msgs.append({"role": "user", "content": message})

        last_err = None
        for model in CLAUDE_MODELS:
            wrote = False
            try:
                with _client.messages.stream(
                    model=model, max_tokens=4096, messages=msgs,
                ) as stream:
                    for text in stream.text_stream:
                        wrote = True
                        self._chunk(text)
                return  # 成功
            except (anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
                last_err = e
                if wrote:
                    return  # 已经输出过，不再切模型
                continue     # 换下一个模型
            except (BrokenPipeError, OSError):
                return       # 浏览器断开
            except Exception as e:
                self._chunk(f"\n\n[Claude 出错] {type(e).__name__}: {str(e)[:200]}")
                return
        self._chunk(f"\n\n[Claude 无可用模型] {str(last_err)[:200]}")

    # ---------------- agy via CLI ----------------
    def _stream_agy(self, history, model, message):
        m = model if model in AGY_MODELS else DEFAULT_AGY_MODEL
        prompt = build_prompt(history, message)
        cmd = ["agy", f"--prompt={prompt}", "--model", m]
        proc = None
        timed_out = {"v": False}
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=ENV, cwd=HERE)

            def killer():
                timed_out["v"] = True
                try:
                    proc.kill()
                except Exception:
                    pass

            timer = threading.Timer(REPLY_TIMEOUT, killer)
            timer.start()
            fd = proc.stdout.fileno()
            try:
                while True:
                    chunk = os.read(fd, 4096)
                    if not chunk:
                        break
                    self._chunk(chunk)
            finally:
                timer.cancel()
            proc.wait()
            if timed_out["v"]:
                self._chunk("\n\n[已超时中断]")
        except (BrokenPipeError, OSError):
            pass
        except Exception as e:
            self._chunk(f"\n\n[agy 出错] {type(e).__name__}: {str(e)[:200]}")
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"双子对话台 running at http://{HOST}:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
