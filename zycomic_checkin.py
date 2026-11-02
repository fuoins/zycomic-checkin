#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZYComic 三平台签到（禁漫 JM / 哔咔 Picacg / 漫蛙 Manwa）—— 青龙脚本

用法：
  python3 zycomic_checkin.py             # 三个平台依次跑（未配置的自动跳过）
  python3 zycomic_checkin.py jm          # 只跑禁漫，可选 picacg / manwa
  python3 zycomic_checkin.py login       # 漫蛙验证码登录助手：给图片直链，看图输码，拿 Cookie
  python3 zycomic_checkin.py login --no-web   # 无网页模式（青龙里可终端交互时用）

环境变量见 README.md，核心几个：
  JM_USER / JM_PASS
  PICACG_USER / PICACG_PASS
  MANWA_COOKIE（推荐，可由 login 自动生成到 manwa_cookie.txt）
  MANWA_USER / MANWA_PASS（配合验证码助手使用）

依赖：requests、pycryptodome
  pip3 install requests pycryptodome

cron 建议：10 0 * * *
"""

import argparse
import base64
import hashlib
import hmac
import html
import http.client
import json
import os
import queue
import random
import re
import socket
import ssl
import string
import sys
import threading
import time
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import requests

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import unpad
except ImportError:
    print("缺少依赖 pycryptodome，请执行：pip3 install pycryptodome")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TIMEOUT = 25
LOG_SCOPE = {"jm": "禁漫", "picacg": "哔咔", "manwa": "漫蛙", "all": "总览", "login": "登录"}


def log(scope, msg):
    print(f"[{time.strftime('%H:%M:%S')}][{LOG_SCOPE.get(scope, scope)}] {msg}", flush=True)


def env(name, default=""):
    return (os.environ.get(name) or default).strip()


def md5_hex(text):
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def split_hosts(raw):
    return [h.strip().rstrip("/") for h in raw.split(",") if h.strip()]


def notify(title, content):
    try:
        from notify import send  # 青龙自带，未配置渠道时静默跳过
        send(title, content)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 禁漫 JM
# ---------------------------------------------------------------------------

JM_APP_VERSION = "2.0.16"
JM_UA = (
    "Mozilla/5.0 (Linux; Android 10; K; wv) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Version/4.0 Chrome/130.0.0.0 Mobile Safari/537.36"
)
JM_ORIGIN = "https://localhost"
JM_PACKAGE = "com.example.app"
JM_REQUEST_SALT = "18comicAPPContent"
JM_RESPONSE_SALT = "185Hcomic3PAPP7R"
JM_DEFAULT_HOSTS = [
    "https://www.cdnutc.me",
    "https://www.cdnhjk.net",
    "https://www.cdngwc.cc",
    "https://www.cdngwc.net",
    "https://www.cdngwc.club",
]
JM_RETRY = 2


def jm_headers():
    ts = str(int(time.time()))
    return ts, {
        "User-Agent": JM_UA,
        "Origin": JM_ORIGIN,
        "Referer": JM_ORIGIN + "/",
        "X-Requested-With": JM_PACKAGE,
        "token": md5_hex(ts + JM_REQUEST_SALT),
        "tokenparam": f"{ts},{JM_APP_VERSION}",
        "Accept": "application/json, text/plain, */*",
    }


def jm_decrypt(payload, ts):
    """AES-256-ECB/PKCS7；key = md5(ts + RESPONSE_SALT) 的 32 位 hex 字符串按 UTF-8 取字节。"""
    key = md5_hex(ts + JM_RESPONSE_SALT).encode("utf-8")
    raw = base64.b64decode(payload.strip())
    plain = unpad(AES.new(key, AES.MODE_ECB).decrypt(raw), 16).decode("utf-8", "replace")
    start = min([i for i in (plain.find("{"), plain.find("[")) if i >= 0] or [0])
    end = max(plain.rfind("}"), plain.rfind("]"))
    if end < start:
        raise ValueError("解密结果不含 JSON")
    return plain[start:end + 1]


def jm_api(host, method, path, token=None, avs=None, form=None, retry=JM_RETRY):
    """返回 (code, data, msg)；网络异常重试后抛出。"""
    last_error = None
    for attempt in range(retry + 1):
        ts, headers = jm_headers()
        if token:
            headers["Authorization"] = "Bearer " + token
        if avs:
            headers["Cookie"] = "AVS=" + avs
        kwargs = {"headers": headers, "timeout": TIMEOUT}
        if form is not None:
            kwargs["data"] = form
        try:
            resp = requests.request(method, host.rstrip("/") + path, **kwargs)
            if resp.status_code != 200:
                last_error = f"HTTP {resp.status_code}"
                if attempt < retry:
                    time.sleep(2 + attempt * 2)
                    continue
                raise RuntimeError(last_error)
            try:
                envelope = resp.json()
            except ValueError:
                last_error = "响应不是 JSON（可能被风控）"
                if attempt < retry:
                    time.sleep(3 + attempt * 3)
                    continue
                raise RuntimeError(last_error)
            code = envelope.get("code")
            data = envelope.get("data")
            if isinstance(data, str):
                try:
                    data = json.loads(jm_decrypt(data, ts))
                except Exception:  # noqa: BLE001
                    data = None
            msg = envelope.get("errorMsg") or ""
            if isinstance(data, dict):
                msg = data.get("msg") or msg
            return code, data, msg
        except Exception as error:  # noqa: BLE001
            last_error = error
            if attempt < retry:
                time.sleep(2 + attempt * 2)
    raise RuntimeError(str(last_error))


def jm_call(hosts, method, path, **kwargs):
    """按线路顺序尝试，命中即返回。"""
    errors = []
    for host in hosts:
        try:
            return host, jm_api(host, method, path, **kwargs)
        except Exception as error:  # noqa: BLE001
            errors.append(f"{host}: {error}")
            log("jm", f"线路失败 {host}: {error}")
    raise RuntimeError("所有线路均失败：" + " | ".join(errors))


def jm_today_signed(record):
    """record 每格的 date 只有两位「日」，如 01/31。"""
    day = time.strftime("%d")
    for week in record or []:
        for cell in week or []:
            if cell.get("date") == day:
                return bool(cell.get("signed"))
    return False


def jm_month_progress(record):
    signed = total = 0
    for week in record or []:
        for cell in week or []:
            if cell.get("date"):
                total += 1
                if cell.get("signed"):
                    signed += 1
    return signed, total


def run_jm():
    user, password = env("JM_USER"), env("JM_PASS")
    if not user or not password:
        return "skip", "未配置 JM_USER / JM_PASS"

    hosts = split_hosts(env("JM_API_HOST")) or JM_DEFAULT_HOSTS
    today = time.strftime("%Y-%m-%d")
    today_day = time.strftime("%d")
    month_prefix = time.strftime("%Y-%m")

    # 1. 登录
    host, (code, data, msg) = jm_call(hosts, "POST", "/login", form={"username": user, "password": password})
    if code != 200 or not isinstance(data, dict):
        return "fail", f"登录失败 code={code} {msg}"
    token = data.get("jwttoken") or ""
    uid = str(data.get("uid") or "")
    avs = data.get("s") or ""
    if not token or not uid:
        return "fail", "登录响应缺少 jwttoken / uid"
    log("jm", f"登录成功 uid={uid} 线路={host}")
    log("jm", f"账户：{data.get('username')} Lv{data.get('level')} coin={data.get('coin')} exp={data.get('exp')}")

    # 2. 查当月签到日历
    _, (code, daily, msg) = jm_call(hosts, "GET", f"/daily?user_id={uid}", token=token, avs=avs)
    if code != 200 or not isinstance(daily, dict):
        return "fail", f"签到日历获取失败 code={code} {msg}"
    daily_id = str(daily.get("daily_id") or "")
    record = daily.get("record") or []
    signed, total = jm_month_progress(record)
    log("jm", f"活动：{daily.get('event_name') or ''}（daily_id={daily_id}）本月 {signed}/{total}")
    if not daily_id:
        return "fail", "未找到 daily_id，可能当月没有签到活动"

    # 3. 今天是否已签
    if jm_today_signed(record):
        log("jm", f"今天（{today}）已签到，无需重复")
        detail = f"已签，本月 {signed}/{total}"
    else:
        _, (code, data, msg) = jm_call(
            hosts, "POST", "/daily_chk", token=token, avs=avs,
            form={"user_id": uid, "daily_id": daily_id},
        )
        if code != 200:
            return "fail", f"签到失败 code={code} {msg}"
        log("jm", f"签到成功：{msg or 'ok'}")
        detail = f"签到成功，本月 {signed}/{total}"

    # 4. 补签当月漏签（可选）
    if env("JM_TRY_MAKEUP", "1") != "0":
        _, (code, daily2, _) = jm_call(hosts, "GET", f"/daily?user_id={uid}", token=token, avs=avs)
        if code == 200 and isinstance(daily2, dict):
            record2 = daily2.get("record") or []
            missing = []
            for week in record2:
                for cell in week or []:
                    day = cell.get("date")
                    if day and not cell.get("signed") and day < today_day:
                        missing.append(f"{month_prefix}-{day}")
            if missing:
                log("jm", f"发现 {len(missing)} 天漏签：{', '.join(missing)}")
                for date in missing:
                    time.sleep(1.5)
                    _, (c, _d, m) = jm_call(
                        hosts, "POST", "/daily_chk", token=token, avs=avs,
                        form={"user_id": uid, "daily_id": daily_id, "date": date},
                    )
                    log("jm", f"补签 {date} → code={c} {m or ''}")
            else:
                log("jm", "本月无漏签")

    # 5. 复查
    _, (code, final, _) = jm_call(hosts, "GET", f"/daily?user_id={uid}", token=token, avs=avs)
    if code == 200 and isinstance(final, dict):
        signed, total = jm_month_progress(final.get("record") or [])
        ok_today = jm_today_signed(final.get("record") or [])
        log("jm", f"复查：今天{'已签' if ok_today else '未签'}，本月 {signed}/{total}")
        detail = f"本月 {signed}/{total}，今天{'已签' if ok_today else '未签'}"
        if not ok_today:
            return "fail", detail
    return "ok", detail


# ---------------------------------------------------------------------------
# 哔咔 Picacg
# ---------------------------------------------------------------------------

PICACG_API_KEY = "C69BAF41DA5ABD1FFEDC6D2FEA56B"
PICACG_HMAC_KEY = "~d}$Q7$eIni=V)9\\RK/P.RM4;9[7|@/CA}b~OW!3?EV`:<>M7pddUBL5n|0/*Cn"
PICACG_APP_VERSION = "2.3.1.20241111"
PICACG_BUILD = "20250144"
PICACG_UA = "okhttp/3.8.1"
# 注意：www.cdnhjk.net / www.cdngwc.cc 这些是网页站，不是 API。
# 网页站的 /auth/sign-in 对任何账号都返回 {"code":200,"data":[],"errorMsg":"Not legal.auth"}，
# 用它会得到"密码错误"的假象。App 里真正的 API 基地址是下面这两个：
PICACG_DEFAULT_HOSTS = [
    "https://picaapi.go2778.com",
    "https://picaapi.picacomic.com",  # 官方线路，国内多数网络不可达，作兜底
]
CONFIG_URL = "https://zygongyi.de5.net/doh.json"

PICACG_ERROR_HINTS = {
    "1002": "请求参数不合法（账号或密码为空/含非法字符）",
    "1004": "账号或密码不正确",
    "1005": "登录态失效",
    "1023": "请求过于频繁，被限流，稍后再试",
}

PICACG_HOST = ""
PICACG_UUID = ""
PICACG_HOST_FILE = os.path.join(SCRIPT_DIR, "picacg_host.txt")


def read_cached_host(path):
    """读上次成功的线路，没有就返回空串。"""
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:  # noqa: BLE001
        return ""


def write_cached_host(path, host):
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(host + "\n")
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# 网络路由层：DNS 解析 IP 直连 + SNI 保持域名
# 移植自 ZYComic App 的 PicacgBuiltinNetwork / PicacgSmartDns / PicacgRouteManager：
# IP 直连时 URL、Host、TLS SNI、证书校验全部保持域名不变，只把 TCP 对端换成干净的
# Cloudflare IP，绕开本地 DNS 污染 / 劣质解析。
# 哔咔固定走 IP 直连，不再做代理检测 / 域名竞速（竞速里的域名探测一旦本地 DNS 卡住，
# 会白等几十秒）。候选 IP 顺序：上次成功的最优 IP（缓存）→ SmartDNS 干净 IP 池 →
# 内置 anycast 兜底；当前 IP 超时才轮换下一个，成功后继续沿用并回写缓存。
# ---------------------------------------------------------------------------

PICACG_TIMEOUT = 5  # 哔咔请求超时（秒），不再用全局 TIMEOUT=25 慢等
PICACG_ROUTE_FILE = os.path.join(SCRIPT_DIR, "picacg_route.txt")
PICACG_DNS_FILE = os.path.join(SCRIPT_DIR, "picacg_dns.txt")
PICACG_ROUTE_TTL = int(env("PICACG_ROUTE_TTL") or 86400)  # 最优 IP 缓存 1 天
PICACG_DNS_TTL = int(env("PICACG_DNS_TTL") or 7 * 86400)  # SmartDNS 池缓存 7 天

# 内置根域名 → Cloudflare anycast IP（与 App PicacgBuiltinNetwork.rootRules 一致）
PICACG_BUILTIN_ROOTS = {
    "go2778.com": ["104.21.42.75", "172.67.159.59"],
    "picacomic.com": ["104.20.42.9", "172.66.173.71"],
    "safedataplj.com": ["104.21.75.42", "172.67.213.2"],
}
PICACG_SMARTDNS_URLS = [
    "https://macapi1.com/app/picacomic/dns/resolve?domain=picaapi.picacomic.com",
    "https://macapi3.top/app/picacomic/dns/resolve?domain=picaapi.picacomic.com",
]

# IP 直连模式的解析覆盖表（只含哔咔白名单域名；漫蛙走系统 DNS，不受影响）
FORCED_IPS = {}
_ORIG_GETADDRINFO = socket.getaddrinfo


def _patched_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, str) and host in FORCED_IPS:
        host = FORCED_IPS[host]
    return _ORIG_GETADDRINFO(host, *args, **kwargs)


def dns_patch_installed():
    return socket.getaddrinfo is _patched_getaddrinfo


def install_dns_patch():
    if not dns_patch_installed():
        socket.getaddrinfo = _patched_getaddrinfo


class PinnedConn(http.client.HTTPSConnection):
    """TCP 连到指定 ip，TLS 的 SNI 用 sni（None 表示空 SNI，证书校验随之关闭）。"""

    def __init__(self, domain, ip, timeout=PICACG_TIMEOUT, sni=None):
        super().__init__(domain, timeout=timeout)
        self._ip = ip
        self._sni = sni

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        if self._sni is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
        self.sock = context.wrap_socket(sock, server_hostname=self._sni)


def is_public_ipv4(text):
    """严格 IPv4 字面量 + 公网段过滤（与 App isPublicIpv4 一致）。"""
    parts = text.split(".")
    if len(parts) != 4 or not all(
        p.isdigit() and 0 <= int(p) <= 255 and (p == "0" or not p.startswith("0")) for p in parts
    ):
        return False
    a, b, c, _ = (int(p) for p in parts)
    if a == 0 or a == 10 or a == 127 or a >= 224:
        return False
    if a == 100 and 64 <= b <= 127:
        return False  # 共享地址段
    if a == 169 and b == 254:
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 192 and b == 168:
        return False
    if a == 192 and b == 0 and c in (0, 2):
        return False  # 协议分配 / 文档段
    if a == 192 and b == 88 and c == 99:
        return False  # 废弃 6to4 中继
    if a == 198 and b in (18, 19, 51) and (b != 51 or c == 100):
        return False  # 基准测试 / 文档段
    if a == 203 and b == 0 and c == 113:
        return False  # 文档段
    return True


def picacg_smartdns_ips():
    """SmartDNS 干净 IP 池：TTL 内读缓存，过期拉接口；失败返回空列表（不致命）。"""
    try:
        with open(PICACG_DNS_FILE, encoding="utf-8") as handle:
            obj = json.loads(handle.read())
        if time.time() - float(obj.get("ts") or 0) < PICACG_DNS_TTL:
            ips = [i for i in (obj.get("ips") or []) if is_public_ipv4(i)]
            if ips:
                return ips
    except Exception:  # noqa: BLE001
        pass
    ips = []
    for url in PICACG_SMARTDNS_URLS:
        try:
            resp = requests.get(url, timeout=5, headers={"User-Agent": PICACG_UA, "Accept": "application/json"})
            root = resp.json() if resp.status_code == 200 else {}
            if root.get("code") != 200:
                continue
            lines = (root.get("data") or {}).get("lines") or {}
            polluted = set()
            for line in lines.values():
                polluted.update(line.get("pollutedIps") or [])
            for line in lines.values():
                if str(line.get("status", "")).lower() != "ok":
                    continue
                for ip in line.get("ips") or []:
                    if is_public_ipv4(ip) and ip not in polluted:
                        ips.append(ip)
            if ips:
                break
        except Exception:  # noqa: BLE001
            continue
    if ips:
        try:
            with open(PICACG_DNS_FILE, "w", encoding="utf-8") as handle:
                json.dump({"ts": int(time.time()), "ips": ips[:64]}, handle)
        except Exception:  # noqa: BLE001
            pass
    return ips


def read_route_cache():
    """上次路由结论 (mode, best_ip)；无或过期返回 (None, None)。"""
    try:
        with open(PICACG_ROUTE_FILE, encoding="utf-8") as handle:
            parts = handle.read().strip().split("|")
        if len(parts) >= 3 and time.time() - float(parts[2]) < PICACG_ROUTE_TTL and parts[0] in ("ip", "domain"):
            return parts[0], parts[1]
    except Exception:  # noqa: BLE001
        pass
    return None, None


def write_route_cache(mode, ip):
    try:
        with open(PICACG_ROUTE_FILE, "w", encoding="utf-8") as handle:
            handle.write(f"{mode}|{ip}|{int(time.time())}\n")
    except Exception:  # noqa: BLE001
        pass


def picacg_ip_pool(hostname):
    """hostname 的候选 IP：内置 anycast 按根域名匹配 → SmartDNS 追加 → 缓存最优 IP 无条件置顶。"""
    host = hostname.lower().rstrip(".")
    pool = []
    for root, ips in PICACG_BUILTIN_ROOTS.items():
        if host == root or host.endswith("." + root):
            pool.extend(ips)
    pool.extend(picacg_smartdns_ips())
    ordered, seen = [], set()
    for ip in pool:
        if ip not in seen:
            seen.add(ip)
            ordered.append(ip)
    cached_mode, cached_ip = read_route_cache()
    if cached_mode == "ip" and cached_ip:
        # 上次请求成功的 IP 置顶：即使它已不在 SmartDNS 最新池里，也值得再试一次
        ordered = [cached_ip] + [i for i in ordered if i != cached_ip]
    return ordered


# 路由全局状态：mode=ip/domain；ip 模式维护当前线路的候选池与游标（成功沿用，异常轮换）
ROUTE = {"mode": "ip", "hostname": "", "pool": [], "idx": -1, "ip": ""}


def picacg_bind_ip(host):
    """IP 模式：确保当前线路已绑定候选 IP（首次调用绑定池中第一个，之后沿用当前 IP）。"""
    hostname = (urlparse(host).hostname or host).lower().rstrip(".")
    if ROUTE["hostname"] != hostname or not ROUTE["pool"]:
        ROUTE["hostname"] = hostname
        ROUTE["pool"] = picacg_ip_pool(hostname)
        ROUTE["idx"] = -1
    if ROUTE["idx"] >= 0:
        return True  # 已绑定且未报错，沿用当前 IP，不无谓轮换
    if not ROUTE["pool"]:
        return False
    ROUTE["idx"] = 0
    ROUTE["ip"] = ROUTE["pool"][0]
    FORCED_IPS[hostname] = ROUTE["ip"]
    install_dns_patch()
    return True


def picacg_next_ip():
    """IP 模式：当前 IP 请求异常时轮换到下一个候选；池耗尽返回 False。"""
    if ROUTE["idx"] + 1 >= len(ROUTE["pool"]):
        return False
    ROUTE["idx"] += 1
    ROUTE["ip"] = ROUTE["pool"][ROUTE["idx"]]
    FORCED_IPS[ROUTE["hostname"]] = ROUTE["ip"]
    return True


def picacg_setup_route():
    """哔咔固定 IP 直连，不做代理检测 / 竞速判定；PICACG_ROUTE=domain 可手动退回域名模式。"""
    requested = env("PICACG_ROUTE") or "ip"
    if requested not in ("ip", "domain"):
        requested = "ip"
    ROUTE["mode"] = requested
    if requested == "ip":
        log("picacg", "路由=ip（IP 直连：缓存最优 → SmartDNS → 内置 anycast）")
    else:
        log("picacg", "路由=domain（PICACG_ROUTE 手动指定，走系统 DNS）")
    return requested


def picacg_nonce():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=32))


def picacg_sign(path_and_query, method, ts, nonce_value):
    message = (path_and_query.lstrip("/") + str(ts) + nonce_value + method.lower() + PICACG_API_KEY).lower()
    return hmac.new(PICACG_HMAC_KEY.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()


def picacg_headers(path_and_query, method, token=None):
    ts = int(time.time())
    nonce_value = picacg_nonce()
    headers = {
        "accept": "application/vnd.picacomic.com.v1+json",
        "time": str(ts),
        "nonce": nonce_value,
        "signature": picacg_sign(path_and_query, method, ts, nonce_value),
        "api-key": PICACG_API_KEY,
        "app-channel": "1",
        "app-version": PICACG_APP_VERSION,
        "app-uuid": PICACG_UUID,
        "app-platform": "android",
        "app-build-version": PICACG_BUILD,
        "image-quality": "medium",
        "User-Agent": PICACG_UA,
        "Content-Type": "application/json; charset=UTF-8",
    }
    if token:
        headers["authorization"] = token
    return headers


class PicacgBusinessError(RuntimeError):
    """服务端明确的业务错误：重试没有意义，直接上抛。"""


def picacg_request(method, path_and_query, body=None, token=None, retry=2, strict=True, timeout=PICACG_TIMEOUT):
    """strict=False 时只要拿到合法 JSON 即返回，用于线路探测。

    哔咔的业务错误同样返回 HTTP 200 + code=200，真实错误在 errorMsg / error，
    data 为空数组（如 "Not legal.auth"），因此必须额外校验 data 形状。
    只有网络异常和响应不是 JSON 才重试，账号密码错这类业务错误一次就返回。
    IP 直连模式下，网络异常重试会轮换到下一个候选 IP，而不是对同一个慢 IP 干等。
    """
    last_error = None
    for attempt in range(retry + 1):
        if ROUTE["mode"] == "ip" and not picacg_bind_ip(PICACG_HOST):
            raise RuntimeError(f"网络异常且 IP 池耗尽：{last_error}")
        url = PICACG_HOST.rstrip("/") + path_and_query
        headers = picacg_headers(path_and_query, method, token)
        kwargs = {"headers": headers, "timeout": timeout}
        if method.upper() == "POST":
            kwargs["data"] = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
        try:
            resp = requests.request(method, url, **kwargs)
        except Exception as error:  # noqa: BLE001
            last_error = error
            if attempt < retry:
                if ROUTE["mode"] == "ip" and ROUTE["pool"]:
                    bad_ip = ROUTE["ip"]
                    if not picacg_next_ip():
                        raise RuntimeError(f"网络异常且 IP 池耗尽：{error}") from error
                    log("picacg", f"请求超时/异常（{bad_ip}），换下一个候选 IP 重试")
                time.sleep(1 + attempt)
                continue
            raise RuntimeError(f"网络异常：{error}") from error
        if ROUTE["mode"] == "ip" and ROUTE["ip"]:
            write_route_cache("ip", ROUTE["ip"])  # 请求成功：记下最优 IP，下次直接置顶

        try:
            envelope = resp.json()
        except ValueError:
            last_error = f"响应不是 JSON（HTTP {resp.status_code}）"
            if attempt < retry:
                time.sleep(1 + attempt)
                continue
            raise RuntimeError(last_error)
        if not strict:
            return envelope

        code = envelope.get("code", resp.status_code)
        error_code = str(envelope.get("error") or "")
        message = envelope.get("errorMsg") or envelope.get("message") or ""
        data = envelope.get("data")
        if resp.status_code != 200 or code not in (200, 0):
            hint = PICACG_ERROR_HINTS.get(error_code, "")
            detail = f"HTTP {resp.status_code} code={code}"
            if error_code:
                detail += f" error={error_code}"
            if message:
                detail += f" {message}"
            if hint:
                detail += f"（{hint}）"
            raise PicacgBusinessError(detail)
        if not isinstance(data, dict):
            raise PicacgBusinessError(message or "服务端返回空数据（该线路可能不是 API 线路）")
        return data
    raise RuntimeError(str(last_error))


def picacg_hosts():
    custom = env("PICACG_API_HOST")
    if custom:
        return split_hosts(custom)
    # 先上次成功的线路，再内置线路；远程配置放到全挂之后才拉，免得每次白等两秒以上。
    cached = read_cached_host(PICACG_HOST_FILE)
    return ([cached] if cached else []) + [h for h in PICACG_DEFAULT_HOSTS if h != cached]


def picacg_remote_hosts():
    """远程配置里的 lines2 大多是网页站，只保留含 picaapi 的条目。"""
    hosts = []
    try:
        resp = requests.get(CONFIG_URL, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        for item in resp.json().get("lines2") or []:
            if isinstance(item, str) and "picaapi" in item and item.rstrip("/") not in hosts:
                hosts.append(item.rstrip("/"))
    except Exception as error:  # noqa: BLE001
        log("picacg", f"拉取线路配置失败：{error}")
    return hosts


def picacg_try(host):
    """单线路探测：成功返回 True。"""
    global PICACG_HOST
    PICACG_HOST = host
    envelope = picacg_request("GET", "/comics?page=1", retry=1, strict=False)
    if str(envelope.get("errorMsg") or "") == "Not legal.auth":
        raise RuntimeError("这是网页站，不是 API 线路")
    return True


def picacg_probe(hosts):
    cached = read_cached_host(PICACG_HOST_FILE)
    last_error = None
    tried = []
    for host in hosts:
        tried.append(host)
        try:
            picacg_try(host)
            log("picacg", f"使用线路 {host}" + ("（缓存）" if host == cached else ""))
            write_cached_host(PICACG_HOST_FILE, host)
            return host
        except Exception as error:  # noqa: BLE001
            last_error = error
            if host == cached:
                log("picacg", f"缓存线路失效 {host}: {error}")
            else:
                log("picacg", f"线路不可用 {host}: {error}")
    for host in picacg_remote_hosts():
        if host in tried:
            continue
        try:
            picacg_try(host)
            log("picacg", f"使用备用线路 {host}")
            write_cached_host(PICACG_HOST_FILE, host)
            return host
        except Exception as error:  # noqa: BLE001
            last_error = error
            log("picacg", f"线路不可用 {host}: {error}")
    raise RuntimeError(f"没有可用 API 线路：{last_error}")


def run_picacg():
    global PICACG_UUID
    user, password = env("PICACG_USER"), env("PICACG_PASS")
    if not user or not password:
        return "skip", "未配置 PICACG_USER / PICACG_PASS"

    PICACG_UUID = env("PICACG_APP_UUID") or uuid.uuid4().hex
    picacg_setup_route()  # 固定 IP 直连（PICACG_ROUTE=domain 可手动退回域名模式）
    picacg_probe(picacg_hosts())

    try:
        data = picacg_request("POST", "/auth/sign-in", body={"email": user, "password": password})
    except Exception as error:  # noqa: BLE001
        return "fail", f"登录失败：{error}"
    token = data.get("token") or ""
    if not token:
        return "fail", "登录响应缺少 token"
    log("picacg", "登录成功")

    profile = picacg_request("GET", "/users/profile", token=token).get("user") or {}
    name = profile.get("name") or profile.get("email") or ""
    punched = profile.get("isPunched")
    if isinstance(punched, str):
        punched = punched.lower() == "true"
    log("picacg", f"账户：{name} Lv{profile.get('level')} exp={profile.get('exp')} 今日打卡={punched}")

    if punched is True:
        log("picacg", "今天已经打过卡了")
    else:
        # 打卡是写操作，服务端可能不返回 data（App 同样允许空 data），失败只认 HTTP/业务码。
        picacg_request("POST", "/users/punch-in", body={}, token=token, strict=False)
        log("picacg", "打卡请求已发送")
        time.sleep(1.5)
        profile = picacg_request("GET", "/users/profile", token=token).get("user") or {}
        punched = profile.get("isPunched")
        if isinstance(punched, str):
            punched = punched.lower() == "true"
        log("picacg", "打卡成功" if punched is True else "打卡后状态未确认（服务端可能延迟）")
    return "ok", f"{name} 打卡={'完成' if punched is True else '未确认'}"


# ---------------------------------------------------------------------------
# 漫蛙 Manwa
# ---------------------------------------------------------------------------

MANWA_UA = (
    "Mozilla/5.0 (Linux; Android 16; PLC110) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36 "
    "mwa-1.1.27+1 (Android/16 OnePlus/PLC110)"
)
MANWA_ORIGIN = "http://mseeowpm1.xyz"
MANWA_XTOKEN_SALT = ",jsdaghuiaonfyudsfnkgjdfkdd"
MANWA_RESPONSE_SALT = ",noiusdfy73osadjap012njdsfn"
MANWA_DEFAULT_DEVICE = "asawsdqwefwsnjfiowehnfjuoweisfnuj"
MANWA_DEFAULT_HOSTS = [
    "https://mseeowpm2.cc",
    "https://mseeowpma.cc",
    "https://mseeowpmb.cc",
    "https://etmdcw.cn",
    "https://mseeowpm3.cc",
    "https://mseeowpm4.cc",
]
MANWA_DEVICE = MANWA_DEFAULT_DEVICE
MANWA_AD_TARGET = 5  # 每天点满 5 次广告可领奖励
COOKIE_FILE = env("MANWA_COOKIE_FILE") or os.path.join(SCRIPT_DIR, "manwa_cookie.txt")
STATE_FILE = os.path.join(SCRIPT_DIR, "manwa_login_state.json")
HOST_FILE = os.path.join(SCRIPT_DIR, "manwa_host.txt")


def manwa_build(host, path):
    ts = str(int(time.time() * 1000))
    sep = "&" if "?" in path else "?"
    url = f"{host.rstrip('/')}{path}{sep}facility=android&deviceid={MANWA_DEVICE}&timestamp={ts}"
    headers = {
        "X-Token": md5_hex(ts + MANWA_XTOKEN_SALT),
        "devid": ts,
        "User-Agent": MANWA_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": MANWA_ORIGIN,
        "Referer": MANWA_ORIGIN,
        "Connection": "keep-alive",
    }
    return url, headers, ts


def manwa_decrypt(raw, ts):
    key = md5_hex(ts + MANWA_RESPONSE_SALT).encode("utf-8")
    payload = raw.strip().strip('"').replace("\\/", "/").replace('\\"', '"')
    plain = unpad(AES.new(key, AES.MODE_ECB).decrypt(base64.b64decode(payload)), 16)
    return plain.decode("utf-8", "replace")


def manwa_parse_response(status, headers, raw_bytes, ts, allow_empty):
    """漫蛙响应解析：二进制直通 / 空响应 / JSON / AES 解密，与请求方式无关。"""
    content_type = headers.get("content-type", "")
    if "image" in content_type or "octet-stream" in content_type:
        return {"code": 1, "data": raw_bytes}
    raw = raw_bytes.decode("utf-8", "replace").strip()
    if not raw:
        if allow_empty and status < 400:
            # /ad_click 这类接口成功时就是空响应，不能当失败重试
            return {"code": 1, "data": {}}
        raise RuntimeError(f"空响应（HTTP {status}）")
    try:
        envelope = json.loads(raw)
    except ValueError:
        envelope = None
    if envelope is None or isinstance(envelope, str):
        envelope = json.loads(manwa_decrypt(raw, ts))
    return envelope


def manwa_sni_omit_applies(host):
    """空 SNI 只作用于用户手动配置的线路（App ManwaSniPolicy：显式配置的主机）。"""
    custom = split_hosts(env("MANWA_API_HOST"))
    return bool(custom) and host.rstrip("/") in custom


def manwa_request_sni_omit(host, method, path, json_body=None, retry=2, allow_empty=False, timeout=TIMEOUT):
    """空 SNI 直连：TLS 不发 SNI 且跳过证书校验，仅用于被 SNI 干扰的自定义线路。"""
    hostname = urlparse(host).hostname
    last_error = None
    for attempt in range(retry + 1):
        url, headers, ts = manwa_build(host, path)
        parsed = urlparse(url)
        body = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None
        try:
            ip = socket.getaddrinfo(hostname, 443)[0][4][0]  # 漫蛙走系统 DNS，不受 IP 映射影响
            conn = PinnedConn(hostname, ip, timeout=timeout, sni=None)
            path_and_query = parsed.path + (("?" + parsed.query) if parsed.query else "")
            conn.request(method, path_and_query, body=body, headers=headers)
            resp = conn.getresponse()
            envelope = manwa_parse_response(resp.status, dict(resp.getheaders()), resp.read(), ts, allow_empty)
            conn.close()
            return envelope
        except Exception as error:  # noqa: BLE001
            last_error = error
        if attempt < retry:
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"{method} {path} 失败（空 SNI 直连）：{last_error}")


def manwa_request(session, host, method, path, json_body=None, retry=2, allow_empty=False, timeout=TIMEOUT):
    if env("MANWA_SNI_OMIT") == "1" and manwa_sni_omit_applies(host):
        return manwa_request_sni_omit(host, method, path, json_body, retry, allow_empty, timeout)
    last_error = None
    for attempt in range(retry + 1):
        url, headers, ts = manwa_build(host, path)
        kwargs = {"headers": headers, "timeout": timeout}
        if json_body is not None:
            kwargs["json"] = json_body
        try:
            resp = session.request(method, url, **kwargs)
            return manwa_parse_response(resp.status_code, resp.headers, resp.content, ts, allow_empty)
        except Exception as error:  # noqa: BLE001
            last_error = error
        if attempt < retry:
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"{method} {path} 失败：{last_error}")


def manwa_hosts():
    custom = env("MANWA_API_HOST")
    if custom:
        return split_hosts(custom)
    # 内置线路先试，全挂才去拉远程配置，避免每次多等一个网络请求。
    return list(MANWA_DEFAULT_HOSTS)


def manwa_remote_hosts():
    hosts = []
    try:
        resp = requests.get(CONFIG_URL, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        for item in resp.json().get("lines") or []:
            if isinstance(item, str) and item.startswith("http") and item.rstrip("/") not in hosts:
                hosts.append(item.rstrip("/"))
    except Exception as error:  # noqa: BLE001
        log("manwa", f"拉取线路配置失败：{error}")
    return hosts


def manwa_probe_one(host, timeout=8):
    try:
        result = manwa_request(requests.Session(), host, "GET",
                               "/api/classes/index?page=1&gender=0", retry=0, timeout=timeout)
        if result.get("code") == 1:
            return True, ""
        return False, f"code={result.get('code')}"
    except Exception as error:  # noqa: BLE001
        message = str(error)
        if "ASCII" in message:
            message = "线路返回异常（疑似被劫持/需要代理）"
        return False, message


def manwa_probe_batch(hosts):
    """并发探测一批线路，谁先成功用谁。

    用守护线程而不是线程池：找到线路立刻返回，剩下卡在超时里的探测
    不会拖住脚本退出（线程池会在进程结束时 join）。
    """
    if not hosts:
        return ""
    results = queue.Queue()
    for host in hosts:
        thread = threading.Thread(target=lambda h=host: results.put((h,) + manwa_probe_one(h)), daemon=True)
        thread.start()
    failures = {}
    winner = ""
    deadline = time.time() + 12
    for _ in hosts:
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        try:
            host, ok, message = results.get(timeout=remaining)
        except queue.Empty:
            break
        if ok:
            winner = host
            break
        failures[host] = message
    for host, message in failures.items():
        log("manwa", f"线路不可用 {host}: {message}")
    if winner:
        log("manwa", f"使用线路 {winner}")
        write_cached_host(HOST_FILE, winner)
    return winner


def manwa_probe(hosts):
    cached = read_cached_host(HOST_FILE)
    # 上次成功的线路单独快测一次：正常 1 秒内出结果，也避免并发时线路来回跳。
    if cached in hosts:
        ok, message = manwa_probe_one(cached)
        if ok:
            log("manwa", f"使用线路 {cached}（缓存）")
            return cached
        log("manwa", f"缓存线路失效 {cached}: {message}")
        hosts = [h for h in hosts if h != cached]
    winner = manwa_probe_batch(hosts)
    if winner:
        return winner
    extra = [h for h in manwa_remote_hosts() if h not in hosts]
    if extra:
        log("manwa", f"内置线路全部不通，改试配置里的 {len(extra)} 条备用线路")
        winner = manwa_probe_batch(extra)
        if winner:
            return winner
    raise RuntimeError("没有可用线路")


def manwa_apply_cookie(session, host, cookie_text):
    for part in cookie_text.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key, value = key.strip(), value.strip()
        if key:
            session.cookies.set(key, value, domain=host.split("//")[-1])


def manwa_cookie_text(session):
    return "; ".join(f"{c.name}={c.value}" for c in session.cookies)


def manwa_save_cookie(text):
    try:
        with open(COOKIE_FILE, "w", encoding="utf-8") as handle:
            handle.write(text.strip() + "\n")
        return True
    except Exception as error:  # noqa: BLE001
        log("manwa", f"Cookie 写入文件失败：{error}")
        return False


def manwa_load_cookie():
    if env("MANWA_COOKIE"):
        return env("MANWA_COOKIE")
    try:
        with open(COOKIE_FILE, encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:  # noqa: BLE001
        return ""


def manwa_logged_in(session, host):
    try:
        result = manwa_request(session, host, "GET", "/api/users/info", retry=0)
        data = result.get("data")
        if result.get("code") == 1 and isinstance(data, dict) and data.get("uid"):
            return data
    except Exception:  # noqa: BLE001
        pass
    return None


def manwa_to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def manwa_to_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in ("1", "true", "yes")


def manwa_pick_point(*sources):
    """积分字段各线路不完全一致，按常见键名逐个找。"""
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("point", "points", "score", "integral"):
            if isinstance(source.get(key), (int, str)) and str(source.get(key)).strip() != "":
                return manwa_to_int(source.get(key), None)
    return None


def manwa_parse_month_day(text):
    """从 2026-10-31 / 2026-10-31 00:00:00 / 10-31 / 10/31 / 31 里取 (月, 日)。"""
    value = str(text or "").strip()
    if not value:
        return None, None
    match = re.search(r"(?<!\d)(\d{4})\D{1,3}(\d{1,2})\D{1,3}(\d{1,2})(?!\d)", value)
    if match:
        return int(match.group(2)), int(match.group(3))
    match = re.search(r"(?<!\d)(\d{1,2})\D{1,3}(\d{1,2})(?!\d)", value)
    if match:
        return int(match.group(1)), int(match.group(2))
    match = re.fullmatch(r"(\d{1,2})", value)
    if match:
        return None, int(match.group(1))
    return None, None


def manwa_today_status(sign_list):
    """True=今天已签 / False=今天未签 / None=没匹配到（不下结论）。

    服务端 date 字段格式不统一（有时带时分秒），所以统一抽成月/日比对；
    抽不出来时退回到 index（当月第几天）。
    """
    if not isinstance(sign_list, list):
        return None
    today_month = int(time.strftime("%m"))
    today_day = int(time.strftime("%d"))
    cell = None
    for item in sign_list:
        if not isinstance(item, dict):
            continue
        month, day = manwa_parse_month_day(item.get("date"))
        if day is None and item.get("index") is not None:
            try:
                day = int(str(item.get("index")).strip())
            except ValueError:
                day = None
        if day is None or day != today_day:
            continue
        if month is not None and month != today_month:
            continue
        cell = item
        if month is not None:
            break  # 完整日期命中，最可靠
    if cell is None:
        return None
    status = str(cell.get("status") or "").strip().lower()
    # 实测枚举：signedin=已签、signedtoday=今天刚签、signednot=漏签、""=占位
    if status in ("signedin", "signedtoday", "signed", "1", "true", "done"):
        return True
    if status in ("signednot", "missed"):
        return False
    return None


def manwa_point_checked_today(session, host):
    """福利接口的 sign_list 对不上时，用积分明细兜底：今天有「签到」入账即为已签。"""
    try:
        result = manwa_request(session, host, "GET", "/api/users/point_logs?page=1", retry=1)
    except Exception as error:  # noqa: BLE001
        log("manwa", f"积分明细查询失败：{error}")
        return None
    if result.get("code") != 1:
        return None
    data = result.get("data")
    if not isinstance(data, dict):
        return None
    logs = data.get("list") or []
    if not isinstance(logs, list):
        return None
    today = time.strftime("%Y-%m-%d")
    for item in logs:
        if not isinstance(item, dict):
            continue
        if not str(item.get("create_time") or "").startswith(today):
            continue
        title = str(item.get("title") or "")
        if "未签到" in title or "未簽到" in title:
            continue  # 「“2”天未签到」是补扣积分的记录，别当成签到成功
        if "签到" in title or "簽到" in title or "sign" in title.lower():
            return True
    return None


def manwa_sign_in(session, host):
    month_first = time.strftime("%Y-%m-01")
    result = manwa_request(session, host, "POST", "/api/users/welfare", json_body={"date": month_first})
    if result.get("code") != 1:
        log("manwa", f"签到返回异常：code={result.get('code')} msg={result.get('msg')}")
        return None
    data = result.get("data") or {}
    user_data = data.get("user_data")
    streak = data.get("consecutive_sign")
    if not streak and isinstance(user_data, dict):
        streak = user_data.get("consecutiveDays")
    # 这个响应可能是签到动作执行前的快照，今日状态放到最后复查里判断
    log("manwa", f"签到请求完成：连签={streak or '未知'}")
    return data


def manwa_extract_links(page):
    links = []
    for href in re.findall(r"<a[^>]+href=[\"']([^\"']+)[\"']", page, re.IGNORECASE):
        link = html.unescape(href).strip()
        if not link or link.startswith("#") or link.startswith("javascript"):
            continue
        if "manwa.me" in link or "ad-provider.js" in link:
            continue
        if link not in links:
            links.append(link)
    return links


def manwa_click_ads(session, host, needed):
    result = manwa_request(session, host, "GET", "/api/index/wawaweise")
    page = result.get("data") or ""
    if not isinstance(page, str):
        page = json.dumps(page, ensure_ascii=False)
    links = manwa_extract_links(page)
    if not links:
        log("manwa", "广告页未解析到任何链接，跳过看广告任务")
        return 0

    clicked = 0
    for index in range(needed):
        link = links[index % len(links)]
        path = f"/ad_click?task=1&group=ad_wawaweise&link={quote(link, safe='')}"
        try:
            manwa_request(session, host, "GET", path, retry=1, allow_empty=True)
            clicked += 1
            log("manwa", f"广告点击 {clicked}/{needed}")
        except Exception as error:  # noqa: BLE001
            log("manwa", f"广告点击失败：{error}")
        time.sleep(1)
    return clicked


def manwa_claim_bonus(session, host):
    result = manwa_request(session, host, "POST", "/api/users/welfare", json_body={"action": "ad_bonus"})
    data = result.get("data") or {}
    if result.get("code") == 1 and str(data.get("status")) == "success":
        log("manwa", f"广告奖励领取成功：{data.get('msg') or '+10 积分'}")
        return True
    log("manwa", f"广告奖励领取失败：{data.get('msg') or result.get('msg')}")
    return False


# ---------------------------------------------------------------------------
# 漫蛙验证码登录助手
# ---------------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>漫蛙登录助手</title><style>
:root{color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;background:#0f1115;
padding:16px;color:#e8eaed;font:15px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
.card{width:100%;max-width:420px;background:#171a21;border:1px solid #262b36;border-radius:16px;padding:22px}
h1{margin:0 0 4px;font-size:17px}
p.sub{margin:0 0 14px;color:#8b93a7;font-size:12.5px}
img{width:100%;height:96px;object-fit:contain;background:#fff;border-radius:10px;display:block}
.row{display:flex;gap:8px;margin-top:10px}
input{width:100%;min-width:0;height:44px;border-radius:10px;border:1px solid #2f3646;
background:#0f1218;color:#e8eaed;padding:0 12px;font-size:16px;outline:none}
input.code{letter-spacing:4px;text-align:center;text-transform:uppercase;font-size:19px}
input:focus{border-color:#4c8dff}
button{height:44px;border:0;border-radius:10px;background:#4c8dff;color:#fff;font-size:14px;font-weight:600;cursor:pointer}
button.ghost{background:#242a36;color:#c7cddb;padding:0 14px}
button[disabled]{opacity:.5;cursor:default}
.err{margin-top:12px;padding:10px 12px;border-radius:10px;background:#3a1d21;color:#ff9aa2;font-size:13px}
.ok{margin-top:12px;padding:10px 12px;border-radius:10px;background:#16301f;color:#7ee2a8;font-size:13px}
code{display:block;margin-top:10px;padding:10px;border-radius:10px;background:#0f1218;border:1px solid #2f3646;
font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;word-break:break-all;color:#c7cddb}
.tip{margin-top:14px;color:#7b8394;font-size:12px;line-height:1.7}
</style></head><body><div class="card">{{BODY}}</div></body></html>
"""

LOGIN_FORM = """<h1>漫蛙登录</h1>
<p class="sub">验证码就是下面这张图，看不清点「换一张」</p>
<img id="cap" src="/captcha.png?k={{K}}&amp;t=0" alt="captcha" onclick="refresh()">
<div class="row"><button class="ghost" type="button" onclick="refresh()">换一张</button>
<span style="flex:1"></span>{{ACCOUNT}}</div>
<form method="post" action="/login">
<input type="hidden" name="k" value="{{K}}">
{{CRED}}
<div class="row"><input class="code" name="code" maxlength="6" autocomplete="off" placeholder="验证码" autofocus></div>
<div class="row"><button type="submit" style="width:100%">登录</button></div>
</form>
{{ERROR}}

<p class="tip">登录成功后页面会给出 Cookie，填进青龙环境变量 <b>MANWA_COOKIE</b> 即可；脚本也会自动存一份到 manwa_cookie.txt。</p>
<script>
function refresh(){var i=document.getElementById('cap');i.src='/captcha.png?k={{K}}&t='+Date.now();}
document.querySelector('form').addEventListener('submit',function(){setTimeout(refresh,1200);});
</script>"""

LOGIN_OK = """<h1>登录成功</h1>
<div class="ok">已拿到登录态，复制下面这串填进环境变量 MANWA_COOKIE（不填也行，已自动保存到本地文件）：</div>
<code id="ck">{{COOKIE}}</code>
<div class="row"><button type="button" onclick="copy()">复制 Cookie</button></div>
<p class="tip">现在可以关闭本页面，回到青龙运行 <b>python3 zycomic_checkin.py</b>。</p>
<script>
function copy(){var t=document.getElementById('ck').innerText;
if(navigator.clipboard){navigator.clipboard.writeText(t).then(function(){alert('已复制');});}
else{var r=document.createRange();r.selectNode(document.getElementById('ck'));getSelection().removeAllRanges();
getSelection().addRange(r);document.execCommand('copy');alert('已复制');}}
</script>"""


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except Exception:  # noqa: BLE001
        return "127.0.0.1"
    finally:
        sock.close()


class ManwaLogin:
    """维护一次登录所需的状态：当前验证码图片、账号密码、结果。"""

    def __init__(self, session, host, user, password):
        self.session = session
        self.host = host
        self.user = user
        self.password = password
        self.fixed = bool(user and password)  # 账号密码来自环境变量/参数时，页面不再显示输入框
        self.last_user = user
        self.last_password = password
        self.image = b""
        self.error = ""
        self.cookie = ""
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.token = uuid.uuid4().hex[:10]

    def refresh_captcha(self):
        """拉一张新验证码；接口若返回图片直链则下载下来。"""
        with self.lock:
            result = manwa_request(self.session, self.host, "GET", "/api/account/captcha", retry=1)
            data = result.get("data")
            if isinstance(data, (bytes, bytearray)) and data:
                self.image = bytes(data)
            elif isinstance(data, str) and data.startswith("http"):
                self.image = requests.get(data, timeout=TIMEOUT).content
            elif isinstance(data, str) and len(data) > 200:
                self.image = base64.b64decode(data)
            else:
                raise RuntimeError("验证码接口返回异常，无法解析图片")
            return self.image

    def submit(self, code, user=None, password=None):
        user = (user or self.user or "").strip()
        password = password or self.password or ""
        if not user or not password:
            return False, "缺少账号或密码（可用 --user / --pass 传入，或设置 MANWA_USER / MANWA_PASS）"
        if not code:
            return False, "请输入验证码"
        self.last_user, self.last_password = user, password
        result = manwa_request(
            self.session, self.host, "POST", "/api/account/login",
            json_body={"username": user, "password": password, "captcha": code.strip()},
        )
        if result.get("code") == 1:
            self.cookie = manwa_cookie_text(self.session)
            self.user, self.password = user, password
            return True, "登录成功"
        log("login", f"登录失败：{result.get('msg') or '未知错误'}")
        return False, str(result.get("msg") or "登录失败")

    def save(self):
        manwa_save_cookie(self.cookie)
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as handle:
                json.dump({"host": self.host, "cookies": self.session.cookies.get_dict(),
                           "time": int(time.time())}, handle)
        except Exception:  # noqa: BLE001
            pass


def manwa_login_web(login, bind, port):
    """起一个本地小页面：直链显示验证码图片 + 输入框，提交后完成登录。"""

    def render_form(error=""):
        cred = ""
        if not login.fixed:
            cred = (
                '<div class="row"><input type="text" name="user" placeholder="账号" autocomplete="username"'
                f' value="{html.escape(login.last_user)}"></div>'
                '<div class="row"><input type="password" name="password" placeholder="密码"'
                f' autocomplete="current-password" value="{html.escape(login.last_password)}"></div>'
            )
        account = (f'<span class="sub" style="margin:12px 0 0">账号：{html.escape(login.last_user)}</span>'
                   if login.fixed else "")
        page = (LOGIN_FORM.replace("{{K}}", login.token)
                .replace("{{ACCOUNT}}", account)
                .replace("{{CRED}}", cred)
                .replace("{{ERROR}}", f'<div class="err">{html.escape(error)}</div>' if error else ""))
        return LOGIN_PAGE.replace("{{BODY}}", page)

    def render_ok():
        page = LOGIN_OK.replace("{{COOKIE}}", html.escape(login.cookie))
        return LOGIN_PAGE.replace("{{BODY}}", page)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ManwaLogin/1.0"

        def log_message(self, *args):  # 静默
            return

        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            raw = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(raw)

        def _token_ok(self, token):
            return token == login.token

        def do_GET(self):  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if not self._token_ok((query.get("k") or [""])[0]):
                self._send(403, "forbidden")
                return
            if parsed.path == "/captcha.png":
                try:
                    image = login.refresh_captcha()
                except Exception as error:  # noqa: BLE001
                    self._send(500, str(error), "text/plain; charset=utf-8")
                    return
                self._send(200, image, "image/png")
                return
            if parsed.path == "/":
                self._send(200, render_ok() if login.done.is_set() else render_form())
                return
            self._send(404, "not found")

        def do_POST(self):  # noqa: N802
            parsed = urlparse(self.path)
            length = int(self.headers.get("content-length") or 0)
            form = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            pick = lambda key: (form.get(key) or [""])[0]  # noqa: E731
            if parsed.path != "/login" or not self._token_ok(pick("k")):
                self._send(403, "forbidden")
                return
            ok, message = login.submit(pick("code"), pick("user") or None, pick("password") or None)
            if ok:
                login.save()
                login.done.set()
                self._send(200, render_ok())
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                try:
                    login.refresh_captcha()
                except Exception:  # noqa: BLE001
                    pass
                self._send(200, render_form(message))

    server = None
    for candidate in range(port, port + 10):
        try:
            server = ThreadingHTTPServer((bind, candidate), Handler)
            server.daemon_threads = True
            break
        except OSError:
            continue
    if server is None:
        raise RuntimeError(f"端口 {port}~{port + 9} 都被占用，换一个 --port")

    actual_port = server.server_address[1]
    host_for_link = "127.0.0.1" if bind in ("127.0.0.1", "localhost") else lan_ip()
    page_url = f"http://{host_for_link}:{actual_port}/?k={login.token}"
    image_url = f"http://{host_for_link}:{actual_port}/captcha.png?k={login.token}"
    log("login", f"站点线路：{login.host}")
    log("login", f"验证码图片直链：{image_url}")
    log("login", f"看图输码页面：{page_url}")
    if bind not in ("127.0.0.1", "localhost"):
        log("login", "提示：手机与运行脚本的机器需在同一网络，且端口要对宿主机放行")

    threading.Thread(target=server.serve_forever, daemon=True).start()
    if bind in ("127.0.0.1", "localhost") and env("MANWA_NO_BROWSER", "0") != "1":
        try:
            is_root = hasattr(os, "geteuid") and os.geteuid() == 0
            has_desktop = (sys.platform.startswith("win") or sys.platform == "darwin"
                           or os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
            if has_desktop and not is_root:  # 容器里 root 拉起浏览器必定失败，直接给链接
                webbrowser.open(page_url)
        except Exception:  # noqa: BLE001
            pass
    try:
        login.done.wait(timeout=600)
    finally:
        server.shutdown()
        server.server_close()
    return login.done.is_set()


def manwa_save_state(login):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({"host": login.host, "cookies": login.session.cookies.get_dict(),
                       "time": int(time.time())}, handle)
        return True
    except Exception as error:  # noqa: BLE001
        log("login", f"会话保存失败：{error}")
        return False


def manwa_submit_saved(host, user, password):
    """两步式第二步：用上一轮保存的会话 + MANWA_CAPTCHA 完成登录。"""
    code = env("MANWA_CAPTCHA")
    if not code:
        return None
    if not user or not password:
        log("login", "设置了 MANWA_CAPTCHA，但缺少 MANWA_USER / MANWA_PASS，无法提交")
        return None
    try:
        with open(STATE_FILE, encoding="utf-8") as handle:
            state = json.load(handle)
    except Exception:  # noqa: BLE001
        return None
    session = requests.Session()
    for key, value in (state.get("cookies") or {}).items():
        session.cookies.set(key, value, domain=host.split("//")[-1])
    login = ManwaLogin(session, host, user, password)
    ok, message = login.submit(code)
    if not ok:
        return None
    log("login", message)
    login.save()
    return session


def manwa_login_console(login):
    """无网页模式：图片存本地 + 终端输入验证码；非交互时走两步式（MANWA_CAPTCHA）。"""
    image_path = os.path.join(SCRIPT_DIR, "manwa_captcha.png")

    def dump(image):
        try:
            with open(image_path, "wb") as handle:
                handle.write(image)
            return True
        except Exception as error:  # noqa: BLE001
            log("login", f"验证码保存失败：{error}")
            return False

    dump(login.refresh_captcha())

    if not sys.stdin.isatty():
        # 青龙里没有可交互终端：把会话存下来，用户看图后设 MANWA_CAPTCHA 再跑一次
        manwa_save_state(login)
        log("login", f"验证码图片已保存：{image_path}")
        log("login", "当前没有可交互终端，请：1) 打开上面的图片看验证码；"
                     "2) 设置环境变量 MANWA_CAPTCHA=四位验证码；3) 重新运行本脚本")
        return False

    for _ in range(5):
        try:
            code = input("请输入图片中的验证码（直接回车换一张）：").strip()
        except EOFError:
            return False
        if not code:
            dump(login.refresh_captcha())
            log("login", f"已换一张：{image_path}")
            continue
        ok, message = login.submit(code)
        if ok:
            login.save()
            return True
        dump(login.refresh_captcha())
        log("login", f"已换一张验证码：{image_path}")
    return False


def manwa_login_flow(host, user, password, use_web, bind, port):
    session = manwa_submit_saved(host, user, password)
    if session is not None:
        log("login", "已用 MANWA_CAPTCHA 完成登录")
        log("login", "Cookie：" + manwa_cookie_text(session))
        log("login", f"已保存到 {COOKIE_FILE}")
        return session

    session = requests.Session()
    login = ManwaLogin(session, host, user, password)
    ok = manwa_login_web(login, bind, port) if use_web else manwa_login_console(login)
    if not ok:
        log("login", "登录未完成")
        return None
    log("login", "Cookie：" + login.cookie)
    log("login", f"已保存到 {COOKIE_FILE}")
    return login.session


def manwa_ensure_session(host, allow_interactive):
    """依次尝试：环境变量 Cookie → 本地文件 Cookie → 两步式验证码 → 交互登录。"""
    user, password = env("MANWA_USER"), env("MANWA_PASS")
    cookie_text = manwa_load_cookie()
    if cookie_text:
        session = requests.Session()
        manwa_apply_cookie(session, host, cookie_text)
        info = manwa_logged_in(session, host)
        if info:
            log("manwa", f"Cookie 有效：{info.get('name') or info.get('username') or info.get('uid')}")
            return session
        log("manwa", "Cookie 已失效")

    # 两步式：上一步已拉过验证码并保存会话，这一步带着验证码提交
    session = manwa_submit_saved(host, user, password)
    if session is not None:
        return session

    # 可选：ddddocr 自动识别
    if env("MANWA_CAPTCHA_OCR", "0") == "1" and user and password:
        try:
            import ddddocr  # noqa: F811
            session = requests.Session()
            login = ManwaLogin(session, host, user, password)
            image = login.refresh_captcha()
            code = ddddocr.DdddOcr(show_ad=False).classification(image).strip()
            log("manwa", f"验证码识别结果：{code}")
            ok, message = login.submit(code)
            log("manwa", message)
            if ok:
                login.save()
                return session
        except Exception as error:  # noqa: BLE001
            log("manwa", f"OCR 登录失败：{error}")

    if allow_interactive:
        return manwa_login_flow(host, user, password, True, "127.0.0.1", 8899)

    log("manwa", "需要先登录：运行 python3 zycomic_checkin.py login（给图片直链，看图输码）")
    return None


def run_manwa(allow_interactive=False):
    global MANWA_DEVICE
    if not (manwa_load_cookie() or (env("MANWA_USER") and env("MANWA_PASS"))):
        return "skip", "未配置 MANWA_COOKIE / MANWA_USER+MANWA_PASS"
    MANWA_DEVICE = env("MANWA_DEVICE_ID") or MANWA_DEFAULT_DEVICE
    host = manwa_probe(manwa_hosts())
    session = manwa_ensure_session(host, allow_interactive)
    if session is None:
        return "fail", "未登录（Cookie 失效且无法自动登录）"

    data = manwa_sign_in(session, host)
    if data is None:
        return "fail", "签到失败"

    ad_clicks = manwa_to_int(data.get("ad_clicks"))
    bonus_ready = manwa_to_bool(data.get("check_ad_bonus_today"))
    want_ad = env("MANWA_AD_BONUS", "1") != "0"

    if want_ad and not bonus_ready:
        needed = max(MANWA_AD_TARGET - ad_clicks, 0)
        log("manwa", f"今日广告进度 {ad_clicks}/{MANWA_AD_TARGET}，需点击 {needed} 次")
        if needed > 0:
            manwa_click_ads(session, host, needed)
            time.sleep(2)
        manwa_claim_bonus(session, host)
    elif bonus_ready:
        log("manwa", "今日广告奖励已领取")
    else:
        log("manwa", "已关闭看广告任务（MANWA_AD_BONUS=0）")

    result = manwa_request(session, host, "POST", "/api/users/welfare",
                           json_body={"date": time.strftime("%Y-%m-01")})
    detail = (result.get("data") or {}) if result.get("code") == 1 else {}
    point = manwa_pick_point(detail.get("user_data"), detail, data.get("user_data"), data)
    if point is None:
        # 福利接口不带积分时，回落到用户信息接口
        info = manwa_logged_in(session, host)
        point = manwa_pick_point(info) if info else None
    today_status = manwa_today_status(detail.get("sign_list")) if detail else None
    if today_status is None:
        # 签到接口的 sign_list 有时对不上（格式变/没返列表），用积分明细再确认一次
        fallback = manwa_point_checked_today(session, host)
        if fallback is not None:
            today_status = fallback
            log("manwa", "签到状态以积分明细为准：今日已签")
    if today_status is None and isinstance(detail.get("sign_list"), list) and detail.get("sign_list"):
        items = detail["sign_list"]
        sample = "; ".join(str(item) for item in items[:2])
        log("manwa", f"签到状态未识别，sign_list 共 {len(items)} 天，样例：{sample[:150]}")
    today_text = "已签" if today_status else ("未签" if today_status is False else "未知")
    clicks = manwa_to_int(detail.get("ad_clicks")) if detail else manwa_to_int(data.get("ad_clicks"))
    bonus_text = "已领" if manwa_to_bool(detail.get("check_ad_bonus_today")) else "未领"
    log("manwa", f"复查：积分={point if point is not None else '未知'} 今日={today_text} "
                 f"广告 {clicks}/{MANWA_AD_TARGET} 奖励={bonus_text}")
    if today_status is False:
        log("manwa", "提示：签到接口已成功调用但状态仍显示未签，可能是服务端延迟，稍后可在 App 里确认")

    point_text = f"积分 {point}" if point is not None else "积分未知"
    today_summary = "" if today_status is None else f"，今日{today_text}"
    return "ok", f"{point_text}{today_summary}，广告 {clicks}/{MANWA_AD_TARGET}"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

RUNNERS = {"jm": run_jm, "picacg": run_picacg, "manwa": run_manwa}
STATUS_TEXT = {"ok": "成功", "skip": "跳过", "fail": "失败"}


def run_selected(targets):
    results = {}
    for target in targets:
        log("all", f"===== {LOG_SCOPE[target]} 开始 =====")
        started = time.time()
        try:
            if target == "manwa":
                status, detail = run_manwa(allow_interactive=env("MANWA_INTERACTIVE", "0") == "1")
            else:
                status, detail = RUNNERS[target]()
        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            status, detail = "fail", f"运行异常：{error}"
        results[target] = (status, detail)
        log("all", f"===== {LOG_SCOPE[target]} {STATUS_TEXT[status]}（{time.time() - started:.1f}s）：{detail} =====")
        if target != targets[-1]:
            time.sleep(2)

    lines = [f"{LOG_SCOPE[t]}: [{STATUS_TEXT[s]}] {d}" for t, (s, d) in results.items()]
    log("all", "汇总 ↓")
    for line in lines:
        print("    " + line, flush=True)
    failed = sum(1 for s, _ in results.values() if s == "fail")
    notify("ZYComic 三平台签到", "\n".join(lines))
    return 1 if failed else 0


def main():
    parser = argparse.ArgumentParser(
        description="ZYComic 三平台签到（禁漫 / 哔咔 / 漫蛙）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python3 zycomic_checkin.py            跑全部\n"
               "  python3 zycomic_checkin.py jm         只跑禁漫\n"
               "  python3 zycomic_checkin.py login      漫蛙验证码登录（拿 Cookie）\n"
               "  python3 zycomic_checkin.py login --no-web\n",
    )
    parser.add_argument("action", nargs="?", default="all",
                        choices=["all", "jm", "picacg", "manwa", "login"], help="要执行的动作")
    parser.add_argument("--user", default="", help="漫蛙账号（login 时可用）")
    parser.add_argument("--pass", dest="password", default="", help="漫蛙密码（login 时可用）")
    parser.add_argument("--bind", default="127.0.0.1", help="登录页面监听地址，手机访问用 0.0.0.0")
    parser.add_argument("--port", type=int, default=8899, help="登录页面端口，默认 8899")
    parser.add_argument("--no-web", action="store_true", help="不启动网页，改为终端输入验证码")
    args = parser.parse_args()

    if args.action == "login":
        global MANWA_DEVICE
        MANWA_DEVICE = env("MANWA_DEVICE_ID") or MANWA_DEFAULT_DEVICE
        host = manwa_probe(manwa_hosts())
        user = args.user or env("MANWA_USER")
        password = args.password or env("MANWA_PASS")
        session = manwa_login_flow(host, user, password, not args.no_web, args.bind, args.port)
        if session is None:
            return 1
        info = manwa_logged_in(session, host)
        if info:
            log("login", f"登录态校验通过：{info.get('name') or info.get('username') or info.get('uid')}")
        return 0

    if args.action == "all":
        targets = ["jm", "picacg", "manwa"]
    else:
        targets = [args.action]
    return run_selected(targets)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消", flush=True)
        sys.exit(130)
    except Exception as error:  # noqa: BLE001
        traceback.print_exc()
        notify("ZYComic 签到异常", str(error))
        sys.exit(1)
