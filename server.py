#!/usr/bin/env python3
"""
双子对话台 — 网页上跟 Claude 或 agy(Gemini) 聊天。零第三方 Web 框架，只用标准库起服务。

两侧接法（混合）：
  - Claude 侧: anthropic SDK，经环境里的 ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL 代理，
               模型 global.anthropic.claude-opus-4-8（带兜底）。
  - agy 侧:    `agy --prompt=<...> --model <...>` CLI（复用你的 OAuth 登录），子进程流式。
回复以 HTTP chunked 实时回传浏览器。

安全与认证特性：
  - 访问密码保护 (AUTH_PASSWORD)，防暴力破解限流保护 (Rate Limiting)
  - HMAC-SHA256 签名 Session Token (支持 HttpOnly Cookie 与 Bearer Token)
  - 防时序攻击 (hmac.compare_digest)
  - 子进程安全流控与客户端断开即时销毁
"""
import os
import re
import json
import time
import hmac
import hashlib
import secrets
import subprocess
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import anthropic

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8765"))
HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("DATA_DIR", HERE)
os.makedirs(DATA_DIR, exist_ok=True)

ENV = dict(os.environ)
ENV["PATH"] = "/usr/local/bin" + os.pathsep + os.path.expanduser("~/.local/bin") + os.pathsep + ENV.get("PATH", "")

# ---------------- 安全与鉴权配置 ----------------
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "").strip()
AUTH_SECRET = os.environ.get("AUTH_SECRET", "").strip()
TOKEN_MAX_AGE = int(os.environ.get("TOKEN_MAX_AGE", str(14 * 86400)))  # 默认 14 天有效
COOKIE_NAME = "agy_chat_session"

# 服务端签名密钥：持久化在本地 .server_secret 防止重启后 token 失效
SECRET_FILE = os.path.join(DATA_DIR, ".server_secret")
if not AUTH_SECRET:
    # 优先从 DATA_DIR 读，若无则尝试从 HERE 兼容读取
    target_secret = SECRET_FILE if os.path.exists(SECRET_FILE) else os.path.join(HERE, ".server_secret")
    if os.path.exists(target_secret):
        try:
            with open(target_secret, "r", encoding="utf-8") as f:
                AUTH_SECRET = f.read().strip()
        except Exception:
            pass
    if not AUTH_SECRET:
        AUTH_SECRET = secrets.token_hex(32)
        try:
            with open(SECRET_FILE, "w", encoding="utf-8") as f:
                f.write(AUTH_SECRET)
            os.chmod(SECRET_FILE, 0o600)
        except Exception:
            pass

# 访问密码：优先读环境变量；若未配置则生成随机密码写入 .auth_password
PASSWD_FILE = os.path.join(DATA_DIR, ".auth_password")
if not AUTH_PASSWORD:
    target_passwd = PASSWD_FILE if os.path.exists(PASSWD_FILE) else os.path.join(HERE, ".auth_password")
    if os.path.exists(target_passwd):
        try:
            with open(target_passwd, "r", encoding="utf-8") as f:
                AUTH_PASSWORD = f.read().strip()
        except Exception:
            pass
    if not AUTH_PASSWORD:
        AUTH_PASSWORD = secrets.token_urlsafe(12)
        try:
            with open(PASSWD_FILE, "w", encoding="utf-8") as f:
                f.write(AUTH_PASSWORD)
            os.chmod(PASSWD_FILE, 0o600)
        except Exception:
            pass
        print("\n" + "=" * 58)
        print("🔒 [安全提示] 未检测到 AUTH_PASSWORD 环境变量！")
        print(f"🔑 自动生成访问密码（保存在 {PASSWD_FILE}）：\n   {AUTH_PASSWORD}")
        print("💡 建议在环境或启动命令中显式配置: export AUTH_PASSWORD=\"你的密码\"")
        print("=" * 58 + "\n")

# ---------------- 防暴力破解限流器 ----------------
_failed_attempts = {}
_auth_lock = threading.Lock()
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 900  # 连续错误 5 次锁定 15 分钟

def get_client_ip(handler):
    xff = handler.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    xri = handler.headers.get("X-Real-IP")
    if xri:
        return xri.strip()
    return handler.client_address[0]

def check_ip_locked(ip):
    now = time.time()
    with _auth_lock:
        attempts = [t for t in _failed_attempts.get(ip, []) if now - t < LOCKOUT_SECONDS]
        _failed_attempts[ip] = attempts
        if len(attempts) >= MAX_FAILED_ATTEMPTS:
            remaining = int(LOCKOUT_SECONDS - (now - attempts[-1]))
            return True, max(1, remaining)
        return False, 0

def record_failed_attempt(ip):
    now = time.time()
    with _auth_lock:
        attempts = _failed_attempts.get(ip, [])
        attempts.append(now)
        _failed_attempts[ip] = attempts
        remaining = max(0, MAX_FAILED_ATTEMPTS - len(attempts))
        return remaining

def clear_failed_attempts(ip):
    with _auth_lock:
        _failed_attempts.pop(ip, None)

# ---------------- HMAC Token 签名与校验 ----------------
def make_token(expires_in=TOKEN_MAX_AGE):
    exp = int(time.time()) + expires_in
    nonce = secrets.token_hex(8)
    payload_str = f"{exp}:{nonce}"
    sig = hmac.new(AUTH_SECRET.encode("utf-8"), payload_str.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload_str}:{sig}"

def verify_token(token):
    if not token or not isinstance(token, str):
        return False
    parts = token.split(":")
    if len(parts) != 3:
        return False
    exp_str, nonce, sig = parts
    try:
        exp = int(exp_str)
    except ValueError:
        return False
    if time.time() > exp:
        return False
    payload_str = f"{exp_str}:{nonce}"
    expected_sig = hmac.new(AUTH_SECRET.encode("utf-8"), payload_str.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected_sig)

def is_authenticated(handler):
    # 1. 优先检查 Authorization Header
    auth_header = handler.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
        if verify_token(token):
            return True

    # 2. 检查 Cookie
    cookie_header = handler.headers.get("Cookie", "")
    if cookie_header:
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
            if COOKIE_NAME in cookies:
                token = cookies[COOKIE_NAME].value
                if verify_token(token):
                    return True
        except Exception:
            pass
    return False

# ---------------- 模型列表与提示词构建 ----------------
CLAUDE_MODELS = [
    "global.anthropic.claude-opus-4-8",
    "global.anthropic.claude-opus-4-7",
    "global.anthropic.claude-opus-4-6-v1",
    "global.anthropic.claude-haiku-4-5-20251001-v1:0",
]

AGY_MODELS = [
    "gemini-3.1-pro-high",
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-low",
]
DEFAULT_AGY_MODEL = "gemini-3.8-flash-high"

REPLY_TIMEOUT = 240
ANSI_REGEX = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
_client = anthropic.Anthropic()


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

    def _send_json(self, code, data, extra=None):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Length": str(len(body))}
        if extra:
            headers.update(extra)
        self._headers(code, "application/json; charset=utf-8", headers)
        self.wfile.write(body)

    # ---------------- HEAD ----------------
    def do_HEAD(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._headers(200, "text/html; charset=utf-8")
        elif path == "/api/auth-status":
            self._headers(200, "application/json; charset=utf-8")
        elif path == "/config":
            if not is_authenticated(self):
                self._headers(401, "application/json; charset=utf-8")
            else:
                self._headers(200, "application/json; charset=utf-8")
        else:
            self._headers(404, "text/plain")

    # ---------------- GET ----------------
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/api/auth-status":
            auth_ok = is_authenticated(self)
            self._send_json(200, {"authenticated": auth_ok})
        elif path == "/config":
            if not is_authenticated(self):
                self._send_json(401, {"error": "Unauthorized"})
                return
            self._send_json(200, {
                "agyModels": AGY_MODELS,
                "defaultAgyModel": DEFAULT_AGY_MODEL,
                "claudeModels": CLAUDE_MODELS,
                "defaultClaudeModel": CLAUDE_MODELS[0],
            })
        elif path == "/api/workspace/files":
            if not is_authenticated(self):
                self._send_json(401, {"error": "Unauthorized"})
                return
            allowed_files = []
            hidden_prefixes = (".git", ".env", ".auth_password", ".server_secret", "__pycache__", "venv")
            for root, dirs, files in os.walk(HERE):
                dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "venv", "node_modules", "data")]
                for file in files:
                    if file.startswith(hidden_prefixes):
                        continue
                    rel = os.path.relpath(os.path.join(root, file), HERE)
                    try:
                        sz = os.path.getsize(os.path.join(root, file))
                    except OSError:
                        sz = 0
                    allowed_files.append({"name": file, "path": rel, "size": sz})
            self._send_json(200, {"files": sorted(allowed_files, key=lambda x: x["path"])})
        elif path == "/api/workspace/file":
            if not is_authenticated(self):
                self._send_json(401, {"error": "Unauthorized"})
                return
            from urllib.parse import parse_qs, urlparse
            query = parse_qs(urlparse(self.path).query)
            rel_path = query.get("path", [""])[0].strip()
            safe_target = os.path.abspath(os.path.join(HERE, rel_path))
            if not safe_target.startswith(HERE) or os.path.basename(safe_target).startswith((".env", ".auth_password", ".server_secret", ".git")):
                self._send_json(403, {"error": "Forbidden"})
                return
            if not os.path.isfile(safe_target):
                self._send_json(404, {"error": "File not found"})
                return
            try:
                with open(safe_target, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read(500000)
                self._send_json(200, {"path": rel_path, "content": content})
            except Exception as e:
                self._send_json(500, {"error": str(e)})
        elif path == "/api/workspace/git":
            if not is_authenticated(self):
                self._send_json(401, {"error": "Unauthorized"})
                return
            status_out = ""
            diff_out = ""
            try:
                res_status = subprocess.run(["git", "status", "-s"], cwd=HERE, capture_output=True, text=True, timeout=3)
                if res_status.returncode == 0:
                    status_out = res_status.stdout
                res_diff = subprocess.run(["git", "diff"], cwd=HERE, capture_output=True, text=True, timeout=3)
                if res_diff.returncode == 0:
                    diff_out = res_diff.stdout
            except Exception:
                pass
            self._send_json(200, {"status": status_out, "diff": diff_out})
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
        path = self.path.split("?", 1)[0]
        ip = get_client_ip(self)

        # 1. 登录接口
        if path == "/api/login":
            locked, rem_sec = check_ip_locked(ip)
            if locked:
                self._send_json(429, {"ok": False, "error": f"尝试过于频繁，请在 {rem_sec} 秒后再试"})
                return

            try:
                n = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(n) or b"{}")
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"ok": False, "error": "请求格式错误"})
                return

            pwd = str(payload.get("password") or "")
            # 使用 hmac.compare_digest 防时序侧信道攻击
            if hmac.compare_digest(pwd.encode("utf-8"), AUTH_PASSWORD.encode("utf-8")):
                clear_failed_attempts(ip)
                token = make_token()
                cookie_val = f"{COOKIE_NAME}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={TOKEN_MAX_AGE}"
                self._send_json(200, {"ok": True, "token": token}, extra={"Set-Cookie": cookie_val})
            else:
                rem_tries = record_failed_attempt(ip)
                if rem_tries > 0:
                    err_msg = f"访问密码错误，还剩 {rem_tries} 次机会"
                else:
                    err_msg = f"密码错误次数过多，已锁定 {LOCKOUT_SECONDS // 60} 分钟"
                self._send_json(401, {"ok": False, "error": err_msg})
            return

        # 2. 登出接口
        if path == "/api/logout":
            clear_cookie = f"{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
            self._send_json(200, {"ok": True}, extra={"Set-Cookie": clear_cookie})
            return

        # 3. 聊天对话接口（需强认证）
        if path == "/api/chat":
            if not is_authenticated(self):
                self._send_json(401, {"error": "Unauthorized"})
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

            self._headers(200, "text/plain; charset=utf-8", {
                "Transfer-Encoding": "chunked",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no"
            })
            try:
                if agent == "claude":
                    self._stream_claude(history, message, model)
                else:
                    self._stream_agy(history, model, message)
            finally:
                self._end_chunks()
            return

        self._headers(404, "text/plain", {"Content-Length": "0"})

    # ---------------- chunked helpers ----------------
    def _chunk(self, data):
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not data:
            return
        try:
            self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            raise

    def _end_chunks(self):
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass

    # ---------------- Claude via anthropic SDK ----------------
    def _stream_claude(self, history, message, preferred_model=None):
        msgs = [{"role": t.get("role"), "content": t.get("content", "")}
                for t in (history or []) if t.get("role") in ("user", "assistant")]
        msgs.append({"role": "user", "content": message})

        candidates = list(CLAUDE_MODELS)
        if preferred_model and preferred_model in candidates:
            candidates.remove(preferred_model)
            candidates.insert(0, preferred_model)

        last_err = None
        for model in candidates:
            wrote = False
            try:
                with _client.messages.stream(
                    model=model, max_tokens=4096, messages=msgs,
                ) as stream:
                    for text in stream.text_stream:
                        wrote = True
                        self._chunk(text)
                return  # 成功完成
            except (anthropic.PermissionDeniedError, anthropic.NotFoundError) as e:
                last_err = e
                if wrote:
                    return  # 已经输出过，不再切模型
                continue     # 换下一个模型重试
            except (BrokenPipeError, ConnectionResetError, OSError):
                return       # 客户端主动断开/停止
            except Exception as e:
                try:
                    self._chunk(f"\n\n[Claude 出错] {type(e).__name__}: {str(e)[:200]}")
                except Exception:
                    pass
                return
        try:
            self._chunk(f"\n\n[Claude 无可用模型] {str(last_err)[:200]}")
        except Exception:
            pass

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
                    raw_chunk = os.read(fd, 4096)
                    if not raw_chunk:
                        break
                    # 过滤可能由 CLI 输出的 ANSI 终端控制码
                    text = raw_chunk.decode("utf-8", errors="replace")
                    clean_text = ANSI_REGEX.sub("", text)
                    if clean_text:
                        self._chunk(clean_text)
            finally:
                timer.cancel()
            proc.wait()
            if timed_out["v"]:
                self._chunk("\n\n[已超时中断]")
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端点击停止或断开连接，及时终止子进程
            pass
        except Exception as e:
            try:
                self._chunk(f"\n\n[agy 出错] {type(e).__name__}: {str(e)[:200]}")
            except Exception:
                pass
        finally:
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                except Exception:
                    pass


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"双子对话台 running at http://{HOST}:{PORT}")
    print(f"🔒 安全模式已开启：密码认证 + 防暴力破解 + 签名 Token")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
