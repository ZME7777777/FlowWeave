#!/usr/bin/env python3
"""
EasySearch/Kibana 日志查询工具

支持两种后端（自动检测）：
  - EasySearch（HK / SG）：JWT token 认证
  - Kibana（UAT / MAS）：Session cookie 认证

用法:
  python es-query.py [options]          # 查询日志
  python es-query.py --apps            # 列出所有应用名
  python es-query.py --hosts           # 列出所有主机名

示例:
  python es-query.py --host hq-interface-hkeq-product-tomcat --level ERROR --since now-1h
  python es-query.py --keyword "adrInfo error" --since now-30m --size 50
  python es-query.py --env mas --host hq-interface-aggregation-sgp-mas-alihk-product-v2 --level ERROR --since now-6h
"""

import argparse
import os
import copy
import io
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

# 禁用SSL警告
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

DEFAULT_ES_URL   = "https://easysearch-hk.hszq8.com"
DEFAULT_ES_USER  = "mengen.zheng"
DEFAULT_ES_PASS  = "171328339Zme"
DEFAULT_INDEX    = "buzz-service-*"
DEFAULT_CLUSTER  = "infini_default_system_cluster"
DEFAULT_ENV      = "hk"

INDEX_BY_SCENE = {
    "service": "buzz-service-*",
    "access": "buzz-access-*",
    "trace": "buzz-access-*",
    "all": "buzz-*",
}

ENV_PRESETS = {
    "hk": {
        "label": "HK",
        "url": "https://easysearch-hk.hszq8.com",
        "user": "mengen.zheng",
        "password": "171328339Zme",
        "cluster": "infini_default_system_cluster",
        "no_auth": False,
    },
    "sg": {
        "label": "SG",
        "url": "https://easysearch-sg.hszq8.com",
        "user": "mengen.zheng",
        "password": "171328339Zme",
        "cluster": "infini_default_system_cluster",
        "no_auth": False,
    },
    "uat": {
        "label": "UAT",
        "url": "https://global-testing-kibana.hszq8.com",
        "user": "",
        "password": "",
        "cluster": "infini_default_system_cluster",
        "no_auth": True,
    },
    "mas": {
        "label": "MAS",
        "url": "https://mas-kibana-prod.hszq8.com",
        "user": "kibanaro",
        "password": "gjsEdgaz1L3e9l",
        "cluster": "infini_default_system_cluster",
        "no_auth": False,
    },
    "saudi": {
        "label": "Saudi",
        "url": "https://logcenter-prod-ali-saham.hszq8.com",
        "user": "kibanaro",
        "password": "bsgdDu4#guCYsvkF",
        "cluster": "infini_default_system_cluster",
        "no_auth": False,
    },
    "hkeq-main": {
        "label": "HKeq-Main（主站日志中心）",
        "url": "http://hk-logcenter-kibana-prod.hszq8.com",
        "user": "kibanaro",
        "password": "2keQPZuJYeHdRVwW",
        "cluster": "",
        "no_auth": False,
    },
    "saham": {
        "label": "Saham（沙特 Ali 日志中心）",
        "url": "https://logcenter-prod-ali-saham.hszq8.com",
        "user": "kibanaro",
        "password": "bsgdDu4#guCYsvkF",
        "cluster": "",
        "no_auth": False,
    },
}

# 运行时实际使用的值（由 CLI 参数覆盖）
ES_URL  = DEFAULT_ES_URL
ES_USER = DEFAULT_ES_USER
ES_PASS = DEFAULT_ES_PASS
CLUSTER = DEFAULT_CLUSTER


def normalize_es_url(url):
    url = (url or "").rstrip("/")
    if url.endswith("/app"):
        url = url[:-4]
    return url


def get_env_preset(env_name):
    return ENV_PRESETS.get(env_name, ENV_PRESETS[DEFAULT_ENV])


# ─────────────────────────────────────────────
# 认证（自动检测模式）
# ─────────────────────────────────────────────

def login_easysearch():
    """EasySearch JWT 认证，返回 (token, session=None)"""
    resp = requests.post(
        f"{ES_URL}/account/login",
        json={"username": ES_USER, "password": ES_PASS},
        timeout=10,
        verify=False
    )
    resp.raise_for_status()
    return resp.json()["access_token"], None


def login_kibana():
    """Kibana Session 认证，返回 (token=None, session)"""
    session = requests.Session()
    session.verify = False
    session.headers.update({"kbn-xsrf": "true", "Content-Type": "application/json"})
    resp = session.post(
        f"{ES_URL}/internal/security/login",
        json={"providerType": "basic", "providerName": "basic",
              "currentURL": f"{ES_URL}/login",
              "params": {"username": ES_USER, "password": ES_PASS}},
        timeout=10
    )
    resp.raise_for_status()
    return None, session


def login_kibana_noauth():
    """无认证 Kibana，直接返回带 kbn-xsrf 头的 session"""
    session = requests.Session()
    session.verify = False
    session.headers.update({"kbn-xsrf": "true", "Content-Type": "application/json"})
    return None, session


def auto_login(no_auth=False):
    """
    自动检测后端类型：
    - no_auth=True：跳过认证，直接以 Kibana 模式（无 cookie）发请求
    - 否则先尝试 EasySearch，若 404 则切换 Kibana。
    返回 (mode, token_or_session)
      mode: 'easysearch' | 'kibana'
    """
    if no_auth:
        _, session = login_kibana_noauth()
        return "kibana", session
    try:
        token, _ = login_easysearch()
        return "easysearch", token
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            _, session = login_kibana()
            return "kibana", session
        raise
    except requests.exceptions.ConnectionError:
        raise


# ─────────────────────────────────────────────
# 查询
# ─────────────────────────────────────────────

def do_search_easysearch(token, index, body):
    resp = requests.post(
        f"{ES_URL}/elasticsearch/{CLUSTER}/search/ese?timeout=60s",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={"index": index, "body": body},
        timeout=60,
        verify=False
    )
    resp.raise_for_status()
    return resp.json()


def do_search_kibana(session, index, body):
    payload = {"params": {"index": index, "body": body}}

    # 先尝试异步 ese 端点（hk-logcenter-kibana-prod 使用此方式）
    try:
        resp = session.post(f"{ES_URL}/internal/search/ese", json=payload, timeout=60)
        if resp.status_code == 404:
            raise requests.HTTPError(response=resp)
        resp.raise_for_status()
        data = resp.json()
        search_id = data.get("id")
        import time
        for _ in range(30):
            if not data.get("isRunning", False):
                break
            time.sleep(1)
            poll = session.post(
                f"{ES_URL}/internal/search/ese/{search_id}",
                json=payload,
                timeout=60
            )
            poll.raise_for_status()
            data = poll.json()
        return data.get("rawResponse", data)
    except requests.HTTPError:
        pass

    # 降级同步 es 端点
    resp = session.post(
        f"{ES_URL}/internal/search/es",
        json=payload,
        timeout=60
    )
    resp.raise_for_status()
    data = resp.json()
    # Kibana 返回格式：{"rawResponse": {"hits": {...}, ...}}
    return data.get("rawResponse", data)


def do_search(mode, auth, index, body):
    if mode == "easysearch":
        return do_search_easysearch(auth, index, body)
    else:
        return do_search_kibana(auth, index, body)


def extract_message(src):
    return (src.get("msg") or src.get("message") or "").strip()


def infer_scene(args):
    if getattr(args, "scene", None):
        return args.scene
    if getattr(args, "trace", False):
        return "trace"
    if getattr(args, "req_id", None):
        return "trace"
    if getattr(args, "req_type", None) or getattr(args, "sub_type", None):
        return "access"
    if getattr(args, "key", None):
        return "access"
    if getattr(args, "logger", None) or getattr(args, "level", None):
        return "service"
    return "service"


def resolve_index(args):
    if args.index:
        return args.index
    return INDEX_BY_SCENE.get(infer_scene(args), DEFAULT_INDEX)


def build_keyword_filter(keyword):
    return {
        "bool": {
            "should": [
                {"match": {"msg": keyword}},
                {"match": {"message": keyword}},
                {"match": {"key": keyword}},
                {"match": {"reqId": keyword}},
            ],
            "minimum_should_match": 1,
        }
    }


def _wildcard_clause(field, value):
    return {"wildcard": {field: value}}


def load_exclusion_config(path):
    """Load the optional collection-only noise profile without changing ad-hoc queries."""
    try:
        with open(path, encoding="utf-8") as source:
            config = json.load(source)
    except (OSError, json.JSONDecodeError) as error:
        raise argparse.ArgumentTypeError(f"无法读取排除配置 {path}: {error}") from error

    messages = config.get("message_contains", [])
    hostnames = config.get("hostname_wildcards", [])
    if not isinstance(messages, list) or not isinstance(hostnames, list):
        raise argparse.ArgumentTypeError(
            "排除配置必须包含字符串数组 message_contains 和 hostname_wildcards"
        )
    if any(not isinstance(value, str) or not value for value in messages + hostnames):
        raise argparse.ArgumentTypeError("排除配置只允许非空字符串规则")

    return {
        "path": path,
        "profile": config.get("profile", "unnamed"),
        "schema_version": config.get("schema_version"),
        "message_contains": messages,
        "hostname_wildcards": hostnames,
    }


def build_exclusion_clauses(exclusion_config):
    if not exclusion_config:
        return []

    clauses = []
    for phrase in exclusion_config["message_contains"]:
        pattern = f"*{phrase}*"
        clauses.append({
            "bool": {
                "should": [
                    _wildcard_clause("msg.keyword", pattern),
                    _wildcard_clause("message.keyword", pattern),
                ],
                "minimum_should_match": 1,
            }
        })
    for pattern in exclusion_config["hostname_wildcards"]:
        clauses.append(_wildcard_clause("agent.hostname.keyword", pattern))
    return clauses


def build_query(args):
    filters = []
    must_not = build_exclusion_clauses(getattr(args, "exclusion_config", None))

    if args.app:
        filters.append({"match_phrase": {"appName": args.app}})
    if args.server:
        filters.append({"match_phrase": {"appServer": args.server}})
    if args.host:
        if args.host.endswith("*"):
            filters.append({"wildcard": {"agent.hostname.keyword": args.host}})
        else:
            filters.append({"match_phrase": {"agent.hostname": args.host}})
    if args.level:
        filters.append({"match_phrase": {"logLevel": args.level.upper()}})
    if args.logger:
        filters.append({"match_phrase": {"logger": args.logger}})
    if getattr(args, "req_type", None):
        filters.append({"match_phrase": {"reqType": args.req_type}})
    if getattr(args, "sub_type", None):
        filters.append({"match_phrase": {"subType": args.sub_type}})
    if getattr(args, "key", None):
        filters.append({"match_phrase": {"key": args.key}})
    if args.keyword:
        filters.append(build_keyword_filter(args.keyword))
    if args.req_id:
        filters.append({"match_phrase": {"reqId": args.req_id}})

    # 时间范围（默认最近 15 分钟）
    time_range = {"@timestamp": {}}
    time_range["@timestamp"]["gte"] = args.since if args.since else "now-15m"
    time_range["@timestamp"]["lte"] = args.until if args.until else "now"
    time_range["@timestamp"]["format"] = "strict_date_optional_time"
    filters.append({"range": time_range})

    return {
        "query": {"bool": {"filter": filters, "must_not": must_not}},
        "sort": [{"@timestamp": {"order": "desc" if args.desc else "asc"}}],
        "_source": {"excludes": []} if any(k in (args.index or "") for k in ("nginx", "hqmonitor")) else [
            "@timestamp", "msg", "message", "logLevel", "appName",
            "appServer", "agent.hostname", "logger", "thread", "reqId",
            "reqType", "subType", "key", "costTime", "extFields"
        ],
        "size": args.size,
        "from": args.offset,
    }


def build_agg_query(field, size=50):
    return {
        "query": {"match_all": {}},
        "size": 0,
        "aggs": {
            "values": {
                "terms": {"field": field, "size": size}
            }
        }
    }


# ─────────────────────────────────────────────
# 统计与去重（客户端聚合）
# ─────────────────────────────────────────────

def normalize_key(msg):
    """提取 msg 分类键：取首行，去掉 IP/端口/哈希等变量部分，用于分组"""
    first = msg.split('\n')[0].strip()
    k = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?', '<ip>', first)
    k = re.sub(r'\b[0-9a-f]{16,}\b', '<hash>', k)
    k = re.sub(r'\b\d{8,}\b', '<num>', k)
    k = re.sub(r'"sign(?:_time)?"\s*:\s*"[^"]*"', '"sign":"<omit>"', k)
    return k[:120].strip()


# 匹配 Java 全限定异常类名，如 java.lang.NullPointerException
_EXC_FQCN = re.compile(
    r'\b((?:[a-z][a-z0-9_]*\.)+[A-Z][A-Za-z0-9_]*'
    r'(?:Exception|Error|Throwable|Fault))\b'
)
# 匹配 at com.huasheng.xxx(File.java:行号)
_AT_HUASHENG = re.compile(r'^\s*at\s+(com\.huasheng\.[^\s(]+\([^)]*\))')
_AT_NONJDK   = re.compile(r'^\s*at\s+([^\s(]+\([^)]*\))')


def extract_exception_class(msg):
    """
    一级分类键：从 msg 中提取第一个全限定异常类名。
    找不到则降级为首行 [TAG] 标签，再找不到取首行前80字符。
    """
    m = _EXC_FQCN.search(msg)
    if m:
        return m.group(1)
    first = msg.split('\n')[0].strip()
    tag = re.match(r'(\[[^\]]+\])', first)
    return tag.group(1) if tag else first[:80]


def extract_l2_key(msg, exc_class):
    """
    二级分类键：同一异常类内，按「异常消息 + 第一个业务调用位置」细分。
    - 异常消息：ExceptionClass: <detail> 中的 detail 部分（归一化后）
    - 调用位置：优先取第一个 at com.huasheng.* 行，其次取第一个非 JDK at 行
    """
    lines = msg.split('\n')

    # 提取异常消息
    exc_msg = ""
    for line in lines:
        if exc_class in line:
            rest = line[line.find(exc_class) + len(exc_class):]
            if rest.startswith(':'):
                exc_msg = rest[1:].strip()
            break
    exc_msg = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?', '<ip>', exc_msg)
    exc_msg = re.sub(r'\b\d{6,}\b', '<num>', exc_msg)
    exc_msg = exc_msg[:80]

    # 提取第一个业务调用位置
    call_site = ""
    for line in lines:
        m = _AT_HUASHENG.match(line)
        if m:
            call_site = m.group(1)
            break
    if not call_site:
        for line in lines:
            m = _AT_NONJDK.match(line)
            if m and not any(p in m.group(1) for p in ('java.', 'sun.', 'com.sun.', 'javax.')):
                call_site = m.group(1)
                break

    if call_site:
        return f"{exc_msg} @ {call_site}"
    return exc_msg if exc_msg else normalize_key(msg)


def fetch_sample(mode, auth, args, sample_size=500):
    """取样本用于客户端聚合，返回 (hits, total)"""
    a = copy.copy(args)
    a.size   = min(sample_size, 500)
    a.offset = 0
    a.desc   = True
    body = build_query(a)
    body["_source"] = ["@timestamp", "msg", "message"]
    result = do_search(mode, auth, args.resolved_index, body)
    hits  = result.get("hits", {}).get("hits", [])
    raw   = result.get("hits", {}).get("total", 0)
    total = raw.get("value", 0) if isinstance(raw, dict) else raw
    return hits, total


def fmt_time_range(args):
    """格式化查询时间段用于展示（使用用户原始输入，即 UTC+8）"""
    since = getattr(args, '_since_label', None) or args.since or "now-15m"
    until = getattr(args, '_until_label', None) or args.until or "now"
    return f"{since} ～ {until}"


def fetch_all(mode, auth, args, max_docs=10000):
    """
    全量翻页拉取：通过 from/size 分批获取所有文档（最多 max_docs 条）。
    只取 @timestamp 和 msg 字段，返回 (all_hits, total, is_capped)。
    is_capped=True 表示实际数据可能更多（ES 报 gte 或我们主动截断）。
    """
    BATCH = 500
    a = copy.copy(args)
    a.desc   = True
    a.offset = 0

    # 先取总量
    a.size = 0
    count_body = build_query(a)
    count_result = do_search(mode, auth, args.resolved_index, count_body)
    raw_total = count_result.get("hits", {}).get("total", 0)
    if isinstance(raw_total, dict):
        total    = raw_total.get("value", 0)
        relation = raw_total.get("relation", "eq")
    else:
        total    = raw_total
        relation = "eq"

    # ES relation="gte" 说明实际总量 > 10000，我们只能拿到前 max_docs 条
    es_capped = (relation == "gte")
    cap = min(total, max_docs)
    all_hits = []

    for offset in range(0, cap, BATCH):
        a.size   = min(BATCH, cap - offset)
        a.offset = offset
        body = build_query(a)
        body["_source"] = ["@timestamp", "msg", "message"]
        result = do_search(mode, auth, args.resolved_index, body)
        hits   = result.get("hits", {}).get("hits", [])
        if not hits:
            break
        all_hits.extend(hits)
        print(f"\r  分类中... {len(all_hits)}/{cap} 条", end="", file=sys.stderr, flush=True)
        if len(hits) < BATCH:
            break

    print("", file=sys.stderr)
    is_capped = es_capped or (len(all_hits) >= max_docs)
    return all_hits, total, is_capped


def group_hits(hits, key_fn=None):
    """按 key_fn(msg) 对 hits 分组，默认用 normalize_key"""
    if key_fn is None:
        key_fn = normalize_key
    groups = defaultdict(lambda: {
        "count": 0,
        "hours": defaultdict(int),
        "first_ts":   None,
        "last_ts":    None,
        "sample_msg": None,
        "sample_ts":  None,
    })
    for hit in hits:
        src = hit.get("_source", {})
        msg = extract_message(src)
        ts  = src.get("@timestamp", "")
        key = key_fn(msg)
        g   = groups[key]
        g["count"] += 1
        g["hours"][ts[:13]] += 1
        if g["first_ts"] is None or ts < g["first_ts"]: g["first_ts"] = ts
        if g["last_ts"]  is None or ts > g["last_ts"]:  g["last_ts"]  = ts
        if g["sample_msg"] is None:
            g["sample_msg"] = msg
            g["sample_ts"]  = ts
    return groups


def _fmt_hour(ts13):
    """将 UTC 小时前缀（如 '2026-03-24T05'）转为 UTC+8 后格式化（如 '03-24 13h'）"""
    if len(ts13) < 13:
        return ts13
    try:
        dt = datetime.strptime(ts13, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
        return dt.astimezone(_CST).strftime("%m-%d %Hh")
    except Exception:
        return ts13[5:10] + " " + ts13[11:13] + "h"


def print_stat_result(groups, total, sample_count, stat_size, show_sample, label="[HK]", time_range=""):
    note = f"采样 {sample_count}/{total}" if sample_count < total else f"共 {total}"
    top  = sorted(groups.items(), key=lambda x: -x[1]["count"])[:stat_size]
    tr   = f"  时间段: {time_range}" if time_range else ""
    print(f"{label} {note} 条，错误类型 Top {len(top)}：{tr}\n")

    for i, (key, g) in enumerate(top, 1):
        first = _fmt_ts(g["first_ts"])
        last  = _fmt_ts(g["last_ts"])
        pct   = g["count"] * 100 // max(sample_count, 1)
        print(f"{'─' * 72}")
        print(f"【{i}】{g['count']} 次（≈采样 {pct}%）  {first} ～ {last}")
        print(f"     {key[:110]}")

        # 时间分布
        buckets = sorted(g["hours"].items())
        if len(buckets) <= 12:
            dist = "  ".join(f"{_fmt_hour(k)}({v})" for k, v in buckets)
        else:
            head = [(_fmt_hour(k), v) for k, v in buckets[:4]]
            tail = [(_fmt_hour(k), v) for k, v in buckets[-3:]]
            dist = "  ".join(f"{k}({v})" for k, v in head) + "  ...  " + \
                   "  ".join(f"{k}({v})" for k, v in tail)
        print(f"     时间分布: {dist}")

        # 样例
        if show_sample and g["sample_msg"]:
            sts   = _fmt_ts(g["sample_ts"])
            lines = g["sample_msg"].split("\n")
            print(f"     样例 [{sts}]:")
            for line in lines[:3]:
                print(f"       {line[:120]}")
            if len(lines) > 3:
                print(f"       ...（共 {len(lines)} 行）")

    print(f"{'─' * 72}")


def print_classify_deep_result(l1_groups, l2_map, total, processed, is_capped, show_sample, label="[HK]", time_range=""):
    """
    两级分类结果输出：
    l1_groups : {exc_class -> group_info}   一级（异常类名）
    l2_map    : {exc_class -> {l2_key -> group_info}}  二级（根因细分）
    """
    cap_note = f"（实际总量 >{total}，已处理前 {processed} 条）" if is_capped else f"（全部 {processed} 条）"
    tr       = f"  时间段: {time_range}" if time_range else ""
    top_l1 = sorted(l1_groups.items(), key=lambda x: -x[1]["count"])
    print(f"{label} {cap_note}，共发现 {len(l1_groups)} 种异常类型：{tr}\n")

    for i, (exc_class, g1) in enumerate(top_l1, 1):
        first = _fmt_ts(g1["first_ts"])
        last  = _fmt_ts(g1["last_ts"])
        pct   = g1["count"] * 100 // max(processed, 1)

        print(f"{'━' * 72}")
        print(f"【{i}】{exc_class}")
        print(f"     {g1['count']} 次（{pct}%）  {first} ～ {last}")

        # 一级时间分布
        buckets = sorted(g1["hours"].items())
        if len(buckets) <= 10:
            dist = "  ".join(f"{_fmt_hour(k)}({v})" for k, v in buckets)
        else:
            head = buckets[:3]; tail = buckets[-3:]
            dist = "  ".join(f"{_fmt_hour(k)}({v})" for k, v in head) + \
                   "  ...  " + "  ".join(f"{_fmt_hour(k)}({v})" for k, v in tail)
        print(f"     时间分布: {dist}")

        # 二级细分
        l2 = l2_map.get(exc_class, {})
        if l2:
            top_l2 = sorted(l2.items(), key=lambda x: -x[1]["count"])
            l2_total = sum(v["count"] for v in l2.values())
            print(f"\n     ┌─ 二级分类（采样 {l2_total} 条，{len(l2)} 种根因）")
            for j, (l2_key, g2) in enumerate(top_l2):
                prefix = "├─" if j < len(top_l2) - 1 else "└─"
                f2 = _fmt_ts(g2["first_ts"])
                l2_line = (l2_key[:100] + "...") if len(l2_key) > 100 else l2_key
                print(f"     {prefix} [{g2['count']}次  {f2}～] {l2_line}")
                if show_sample and g2["sample_msg"]:
                    lines = g2["sample_msg"].split("\n")
                    for ln in lines[:2]:
                        print(f"     │    {ln[:110]}")
                    if len(lines) > 2:
                        print(f"     │    ...（共 {len(lines)} 行）")
        print()

    print(f"{'━' * 72}")
    if is_capped:
        print(f"\n⚠ 实际总量超过 ES 上限 10000 条，以上结果仅覆盖最近 {processed} 条。")
        print(f"  建议缩短时间范围（如 --since now-12h）后重新查询以获取完整数据。")


def print_classify_result(groups, total, processed, is_capped, stat_size, show_sample, label="[HK]", time_range=""):
    """全量分类结果输出"""
    cap_note = f"（实际总量 >{total}，已处理前 {processed} 条）" if is_capped else f"（全部 {processed} 条）"
    tr       = f"  时间段: {time_range}" if time_range else ""
    top = sorted(groups.items(), key=lambda x: -x[1]["count"])[:stat_size]
    print(f"{label} {cap_note}，共发现 {len(groups)} 种错误类型，Top {len(top)}：{tr}\n")

    for i, (key, g) in enumerate(top, 1):
        first = _fmt_ts(g["first_ts"])
        last  = _fmt_ts(g["last_ts"])
        pct   = g["count"] * 100 // max(processed, 1)
        print(f"{'─' * 72}")
        print(f"【{i}】{g['count']} 次（{pct}%）  {first} ～ {last}")
        print(f"     {key[:110]}")

        # 时间分布
        buckets = sorted(g["hours"].items())
        if len(buckets) <= 12:
            dist = "  ".join(f"{_fmt_hour(k)}({v})" for k, v in buckets)
        else:
            head = [(_fmt_hour(k), v) for k, v in buckets[:4]]
            tail = [(_fmt_hour(k), v) for k, v in buckets[-3:]]
            dist = "  ".join(f"{k}({v})" for k, v in head) + "  ...  " + \
                   "  ".join(f"{k}({v})" for k, v in tail)
        print(f"     时间分布: {dist}")

        # 样例
        if show_sample and g["sample_msg"]:
            sts   = _fmt_ts(g["sample_ts"])
            lines = g["sample_msg"].split("\n")
            print(f"     样例 [{sts}]:")
            for line in lines[:3]:
                print(f"       {line[:120]}")
            if len(lines) > 3:
                print(f"       ...（共 {len(lines)} 行）")

    print(f"{'─' * 72}")
    if is_capped:
        print(f"\n⚠ 实际总量超过 ES 上限 10000 条，以上结果仅覆盖最近 {processed} 条。")
        print(f"  建议缩短时间范围（如 --since now-12h）后重新查询以获取完整数据。")


def print_dedup_result(groups, total, sample_count, size, label="[HK]", time_range=""):
    note = f"采样 {sample_count}/{total}" if sample_count < total else f"共 {total}"
    top  = sorted(groups.items(), key=lambda x: -x[1]["count"])[:size]
    tr   = f"  时间段: {time_range}" if time_range else ""
    print(f"{label} {note} 条，去重后 {len(groups)} 种错误，显示 Top {len(top)}：{tr}\n")

    for key, g in top:
        ts    = _fmt_ts(g["sample_ts"])
        first = _fmt_ts(g["first_ts"])
        last  = _fmt_ts(g["last_ts"])
        msg   = g["sample_msg"] or key
        print(f"[{ts}] [共 {g['count']} 次  {first} ~ {last}]")
        lines = msg.split("\n")
        for line in lines[:2]:
            print(f"  {line[:160]}")
        if len(lines) > 2:
            print(f"  ...（共 {len(lines)} 行）")
        print()


# ─────────────────────────────────────────────
# 输出格式化
# ─────────────────────────────────────────────

LEVEL_COLORS = {
    "ERROR": "\033[31m",   # 红
    "WARN":  "\033[33m",   # 黄
    "INFO":  "\033[36m",   # 青
    "DEBUG": "\033[37m",   # 灰
}
RESET = "\033[0m"

_CST = timezone(timedelta(hours=8))


def _from_utc8_to_utc(ts_str):
    """将 UTC+8 绝对时间字符串转为 UTC ISO 格式，传给 ES 使用。
    相对时间（now-*）、已有时区后缀（Z/+xx:xx）直接返回原值。"""
    if not ts_str or ts_str.startswith("now"):
        return ts_str
    if ts_str.endswith("Z") or "+" in ts_str[10:]:
        return ts_str
    try:
        fmt = ("%Y-%m-%dT%H:%M:%S.%f" if "." in ts_str else
               "%Y-%m-%dT%H:%M:%S" if "T" in ts_str else "%Y-%m-%d")
        dt_utc8 = datetime.strptime(ts_str, fmt).replace(tzinfo=_CST)
        return dt_utc8.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return ts_str


def _fmt_ts(ts_str, n=16):
    """将 ES 返回的 UTC @timestamp 转换为 UTC+8，截取前 n 个字符。"""
    if not ts_str:
        return ""
    try:
        s = ts_str.rstrip("Z")
        fmt = "%Y-%m-%dT%H:%M:%S.%f" if "." in s else "%Y-%m-%dT%H:%M:%S"
        dt = datetime.strptime(s[:23], fmt).replace(tzinfo=timezone.utc)
        return dt.astimezone(_CST).strftime("%Y-%m-%d %H:%M:%S")[:n]
    except Exception:
        return ts_str[:n].replace("T", " ")


def format_hit(src, use_color=True, verbose=False):
    ts       = _fmt_ts(src.get("@timestamp", ""), 19)
    level    = (src.get("logLevel") or "").strip().upper().ljust(5)
    app      = src.get("appName", "")
    server   = src.get("appServer", "")
    msg      = extract_message(src)
    hostname = (src.get("agent") or {}).get("hostname", "") or src.get("agent.hostname", "")
    req_type = src.get("reqType", "")
    sub_type = src.get("subType", "")
    key      = src.get("key", "")
    cost     = src.get("costTime", "")
    ext      = src.get("extFields", "")

    color = LEVEL_COLORS.get(level.strip(), "") if use_color else ""
    reset = RESET if use_color else ""

    line = f"[{ts}] {color}[{level}]{reset} [{app}/{server}] {msg}"
    if verbose:
        logger = src.get("logger", "")
        thread = src.get("thread", "")
        req_id = src.get("reqId", "")
        if logger:
            line += f"\n          logger={logger} thread={thread}"
        if req_id:
            line += f"\n          reqId={req_id}"
        if req_type or sub_type or cost != "":
            line += f"\n          reqType={req_type or '-'} subType={sub_type or '-'} costTime={cost if cost != '' else '-'}"
        if key:
            line += f"\n          key={key}"
        if ext:
            line += f"\n          extFields={ext}"
        if hostname:
            line += f"\n          host={hostname}"
    return line


def print_trace_result(hits, total, args, label):
    print(
        f"{label} 场景 trace，索引 {args.resolved_index}，共 {total} 条，"
        f"显示 {args.offset+1}–{args.offset+len(hits)} 条  时间段: {fmt_time_range(args)}\n"
    )
    if args.req_id:
        print(f"reqId: {args.req_id}\n")

    use_color = sys.stdout.isatty()
    for idx, hit in enumerate(hits, 1):
        src = hit.get("_source", {})
        print(f"{idx}. {format_hit(src, use_color=use_color, verbose=True)}")
        print("")


# ─────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="EasySearch/Kibana 日志查询工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )

    parser.add_argument("--env",            choices=sorted(ENV_PRESETS.keys()), default=DEFAULT_ENV,
                        help="环境预设：hk / sg / uat / mas（默认 hk）")

    # 连接参数（可覆盖环境预设）
    parser.add_argument("--es-url",         default=None, help="ES/Kibana 地址；支持直接传 Kibana 的 /app 入口 URL")
    parser.add_argument("--es-user",        default=None, help="登录用户名；默认跟随 --env")
    parser.add_argument("--es-pass",        default=None, help="登录密码")
    parser.add_argument("--cluster",        default=None, help="集群名；默认跟随 --env（仅 EasySearch 模式有效）")

    # 过滤条件
    parser.add_argument("--app",     "-a",  help="应用名过滤（appName），如 hq-interface")
    parser.add_argument("--server",  "-s",  help="服务器过滤（appServer），如 hq-hkeq-product")
    parser.add_argument("--host",           help="主机名过滤；不带 * 精确匹配 agent.hostname，带 * 前缀通配 agent.hostname.keyword，如 hq-interface-hkeq-product-tomcat")
    parser.add_argument("--level",   "-l",  help="日志级别：ERROR / WARN / INFO / DEBUG")
    parser.add_argument("--logger",         help="Java logger 类名过滤")
    parser.add_argument("--keyword", "-k",  help="msg 关键词搜索")
    parser.add_argument("--req-id",         help="reqId 过滤")
    parser.add_argument("--req-type",       help="访问类型过滤：如 dubbo / dubboServiceProvider / dubboServiceConsumer / interface / socket / sql")
    parser.add_argument("--sub-type",       help="访问阶段过滤：如 request / response / execute")
    parser.add_argument("--key",            help="buzz_access key 过滤：方法名、接口名、地址等")
    parser.add_argument(
        "--exclude-config",
        help="可选 JSON 排除配置；仅在调用方显式传入时排除低优先级日志",
    )
    parser.add_argument("--scene",          choices=sorted(INDEX_BY_SCENE.keys()), help="查询场景：service / access / trace / all；未指定时自动推断")
    parser.add_argument("--trace",          action="store_true", help="按 reqId 链路查看 buzz_access trace；默认切到 access 索引并按时间正序")

    # 时间范围
    parser.add_argument("--since",   "-f",  help="开始时间，支持 now-15m / now-1h / 2026-03-09T08:00:00（默认 now-15m）")
    parser.add_argument("--until",   "-t",  help="结束时间（默认 now）")

    # 分页 & 排序
    parser.add_argument("--size",    "-n",  type=int, default=20,  help="返回条数（默认 20，最大 500）")
    parser.add_argument("--offset",  "-p",  type=int, default=0,   help="分页偏移（默认 0）")
    parser.add_argument("--desc",           action="store_true", default=True,
                        help="按时间倒序（默认开启）")
    parser.add_argument("--asc",            action="store_true", help="按时间正序")

    # 输出格式
    parser.add_argument("--json",           action="store_true", help="输出原始 JSON")
    parser.add_argument("--verbose", "-v",  action="store_true", help="显示详细字段（logger/thread/reqId/host）")
    parser.add_argument("--index",   "-i",  default=None, help=f"显式索引名；未指定时按场景自动选择（默认 {DEFAULT_INDEX}）")

    # 认证
    parser.add_argument("--no-auth",        action="store_true", help="跳过认证（用于无需登录的 Kibana 实例）")

    # 聚合模式
    parser.add_argument("--apps",           action="store_true", help="列出所有 appName")
    parser.add_argument("--hosts",          action="store_true", help="列出所有 agent.hostname")

    # 统计模式
    parser.add_argument("--stat",           action="store_true", help="统计模式：采样 500 条快速分类（适合快速预览）")
    parser.add_argument("--stat-size",      type=int, default=20,  help="统计/分类模式返回 Top N 错误类型（默认 20）")
    parser.add_argument("--stat-sample",    action="store_true",   help="统计/分类模式同时展示每种错误的代表样例")
    parser.add_argument("--dedup",          action="store_true",   help="去重模式：每种错误只显示一条代表样本 + 计数")
    parser.add_argument("--classify",       action="store_true",   help="全量分类模式：翻页处理所有文档，按异常类名分类，保证不漏报（较慢）")
    parser.add_argument("--classify-deep",  action="store_true",   help="两级分类：先按异常类名，再对每类做根因细分（最慢，最精确）")
    parser.add_argument("--classify-max",   type=int, default=10000, help="全量分类最多处理条数（默认 10000，ES 上限）")

    args = parser.parse_args()
    args.exclusion_config = load_exclusion_config(args.exclude_config) if args.exclude_config else None

    # 保存用户原始输入（UTC+8）用于展示
    args._since_label = args.since or "now-15m"
    args._until_label = args.until or "now"
    # 将绝对时间从 UTC+8 转为 UTC 再传给 ES
    args.since = _from_utc8_to_utc(args.since)
    args.until = _from_utc8_to_utc(args.until)

    # 先应用环境预设，再由 CLI 参数覆盖
    global ES_URL, ES_USER, ES_PASS, CLUSTER
    preset = get_env_preset(args.env)
    ES_URL = normalize_es_url(preset["url"])
    ES_USER = preset["user"]
    ES_PASS = preset["password"]
    CLUSTER = preset["cluster"]
    args.no_auth = args.no_auth or preset["no_auth"]

    if args.es_url:
        ES_URL = normalize_es_url(args.es_url)
    if args.es_user is not None:
        ES_USER = args.es_user
    if args.es_pass is not None:
        ES_PASS = args.es_pass
    if args.cluster:
        CLUSTER = args.cluster

    args.scene = infer_scene(args)
    args.resolved_index = resolve_index(args)
    if args.exclusion_config:
        print(
            "已应用排除配置 "
            f"{args.exclusion_config['profile']} (v{args.exclusion_config['schema_version']})："
            f"{len(args.exclusion_config['message_contains'])} 条消息规则，"
            f"{len(args.exclusion_config['hostname_wildcards'])} 条主机规则。",
            file=sys.stderr,
        )
    if args.asc:
        args.desc = False
    if args.trace or args.scene == "trace":
        args.desc = False
        if args.size == 20:
            args.size = 100

    try:
        mode, auth = auto_login(no_auth=args.no_auth)
    except Exception as e:
        print(f"登录失败: {e}", file=sys.stderr)
        sys.exit(1)

    # 聚合模式
    if args.apps:
        body    = build_agg_query("appName.keyword")
        result  = do_search(mode, auth, args.resolved_index, body)
        buckets = result.get("aggregations", {}).get("values", {}).get("buckets", [])
        print("应用列表 (appName):")
        for b in buckets:
            print(f"  {b['key']}  ({b['doc_count']} 条)")
        return

    if args.hosts:
        body    = build_agg_query("agent.hostname.keyword")
        result  = do_search(mode, auth, args.resolved_index, body)
        buckets = result.get("aggregations", {}).get("values", {}).get("buckets", [])
        print("主机列表 (agent.hostname):")
        for b in buckets:
            print(f"  {b['key']}  ({b['doc_count']} 条)")
        return

    label = f"[{preset['label']}]"

    # ── 两级深度分类 ───────────────────────────────
    if args.classify_deep:
        # Level 1：全量扫描，按异常类名分组
        print("第一步：全量扫描，按异常类名分类...", file=sys.stderr)
        all_hits, total, is_capped = fetch_all(mode, auth, args, max_docs=args.classify_max)
        l1_groups = group_hits(all_hits, key_fn=extract_exception_class)

        # Level 2：对每个异常类，定向查询后按根因细分
        l2_map = {}
        top_l1 = sorted(l1_groups.items(), key=lambda x: -x[1]["count"])
        print(f"第二步：对 {len(top_l1)} 种异常分别做根因细分...", file=sys.stderr)
        for exc_class, g1 in top_l1:
            a2 = copy.copy(args)
            a2.keyword = exc_class
            l2_hits, _ = fetch_sample(mode, auth, a2, sample_size=500)
            l2_groups = group_hits(l2_hits,
                                   key_fn=lambda msg, ec=exc_class: extract_l2_key(msg, ec))
            l2_map[exc_class] = l2_groups
            print(f"  {exc_class}: {g1['count']}次 → {len(l2_groups)} 种根因", file=sys.stderr)

        print("", file=sys.stderr)
        time_range = fmt_time_range(args)
        print_classify_deep_result(l1_groups, l2_map, total, len(all_hits), is_capped,
                                   show_sample=args.stat_sample, label=label, time_range=time_range)
        return

    # ── 全量一级分类 ───────────────────────────────
    if args.classify:
        all_hits, total, is_capped = fetch_all(mode, auth, args, max_docs=args.classify_max)
        l1_groups = group_hits(all_hits, key_fn=extract_exception_class)
        print_classify_result(l1_groups, total, len(all_hits), is_capped,
                              stat_size=args.stat_size,
                              show_sample=args.stat_sample,
                              label=label, time_range=fmt_time_range(args))
        return

    # ── 统计模式 ──────────────────────────────────
    if args.stat:
        hits, total = fetch_sample(mode, auth, args, sample_size=500)
        groups      = group_hits(hits)
        print_stat_result(groups, total, len(hits),
                          stat_size=args.stat_size,
                          show_sample=args.stat_sample,
                          label=label, time_range=fmt_time_range(args))
        return

    # ── 去重模式 ──────────────────────────────────
    if args.dedup:
        hits, total = fetch_sample(mode, auth, args, sample_size=500)
        groups      = group_hits(hits)
        print_dedup_result(groups, total, len(hits), size=args.size, label=label,
                           time_range=fmt_time_range(args))
        return

    # ── 普通日志查询 ──────────────────────────────
    args.size = min(args.size, 500)
    body      = build_query(args)
    result    = do_search(mode, auth, args.resolved_index, body)

    hits  = result.get("hits", {}).get("hits", [])
    raw_total = result.get("hits", {}).get("total", 0)
    total = raw_total.get("value", 0) if isinstance(raw_total, dict) else raw_total

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.trace or args.scene == "trace":
        print_trace_result(hits, total, args, label)
        return

    print(
        f"{label} 场景 {args.scene}，索引 {args.resolved_index}，共 {total} 条，"
        f"显示 {args.offset+1}–{args.offset+len(hits)} 条  时间段: {fmt_time_range(args)}\n"
    )

    use_color = sys.stdout.isatty()
    for hit in hits:
        src = hit.get("_source", {})
        print(format_hit(src, use_color=use_color, verbose=args.verbose))


if __name__ == "__main__":
    main()
