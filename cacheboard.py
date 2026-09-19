"""Public GitHub ingestion and evidence-based cache issue triage."""
from __future__ import annotations

import calendar
import datetime as dt
import html
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from settings import CONFIG

REPO = CONFIG['repository']
UTC = dt.timezone.utc
MODULES = {"hicache": "HiCache", "unified": "Unified Radix Cache", "hisparse": "HiSparse"}
PATTERNS = {
    "hicache": re.compile(r"hi[-_ ]?cache|hierarchical[-_ ](?:kv[-_ ])?cache|hi_radix|hiradix|pool_host|cache_controller|layerloadingevent|L[23][ -](?:KV[ -])?(?:cache|memcache|storage)", re.I),
    "unified": re.compile(r"unified[ _-]*(?:radix|cache|memory)|unifiedradixcache|tree[ _-]?core|radix[ _-]*cache|mamba[ _-]*radix|prefix[ _-]*cach|SWA[ _-]*radix|replayssm.{0,45}(?:backup|restore|retract|checkpoint)|(?:backup|restore|retract|checkpoint).{0,45}replayssm|(?:mamba|GDN|KDA)[ _-]*(?:state|checkpoint|slot)|(?:CPU|host)[ _-]*(?:backup|restore)|(?:backup|restore)[ _/-]*(?:to |from )?CPU|KV[ _-]*allocator", re.I),
    "hisparse": re.compile(r"hi[ _-]?sparse|hisprase|sparse.{0,24}KV.{0,24}(?:swap|offload)", re.I),
}
TEMPLATE = re.compile(r"checklist|format your code|code style|write document|please use english|thank you for your contribution|describe the purpose|detail the changes|latest PR test|pr-states|searched related|if this pull request|if the issue|issues without|the bug persists", re.I)


if CONFIG.get('modules'):
    MODULES = {key: value['name'] for key, value in CONFIG['modules'].items()}
    PATTERNS = {key: re.compile(value['pattern'], re.I) for key, value in CONFIG['modules'].items()}


def now() -> dt.datetime:
    return dt.datetime.now(UTC)


def stamp(value: dt.datetime | None = None) -> str:
    return (value or now()).isoformat(timespec="seconds").replace("+00:00", "Z")


def months_ago(value: dt.datetime, months: int) -> dt.datetime:
    index = value.year * 12 + value.month - 1 - months
    year, month = divmod(index, 12)
    month += 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def clean_body(body: str) -> str:
    body = re.split(r"<!--\s*pr-states:start\s*-->", body)[0]
    body = re.sub(r"<!--[\s\S]*?-->", "", body)
    body = re.sub(r"<img\b[^>]*>", "[Image — open on GitHub]", body, flags=re.I)
    return body.strip()


def prose(body: str) -> list[str]:
    text = re.sub(r"```[\s\S]*?```", "", clean_body(body))
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:#{1,6}\s*|[-*+]\s*|\d+\.\s*)", "", line).strip()
        if not line or TEMPLATE.search(line) or line.startswith(("[ ]", "[x]", "|", "http", "CC:", "cc:")):
            continue
        line = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", line)
        line = re.sub(r"<[^>]+>", "", line).strip(" *`\r")
        if len(line) >= 35:
            lines.append(html.unescape(line))
    return lines


def classify_modules(title: str, body: str) -> list[str]:
    # References alone do not establish a module relationship.
    text = re.split(r"(?im)^#{1,4}\s+(?:related|references|checklist|review and merge)", clean_body(body))[0]
    text = re.sub(r"https?://\S+", "", text)
    title_hits = [key for key, pattern in PATTERNS.items() if pattern.search(title)]
    hits = title_hits[:]
    for key, pattern in PATTERNS.items():
        count = len(pattern.findall(text))
        if key not in hits and (count >= 2 or (count and not title_hits and len(text) < 2500)):
            hits.append(key)
    return [key for key in MODULES if key in hits]


def priority(title: str, body: str, kind: str) -> tuple[str, str]:
    # Symptoms determine urgency; plans and unrelated code blocks do not.
    text = title + " " + " ".join(prose(body)[:12])
    if re.search(r"\b(?:simulator|benchmark)\b|^test(?:\b|:)|\[test\]", title, re.I):
        return "P3", "测试、基准或模拟器工作；不直接作为 serving 故障或修复的证据。"
    if kind == "rfc" or re.search(r"\[(?:RFC|Feature|Refactor|metrics|docs)\]|\b(?:roadmap|observability|refactor)\b", title, re.I):
        return "P3", "能力、设计或观测类工作；上线前仍需验证正确性与生命周期。"
    if re.search(r"silent(?:ly)? (?:wrong|corrupt|drop|reuse)|corrupt|wrong (?:KV|state|row|output|data)|cross[- ](?:request|tenant)|isola(?:te|tion).*(?:salt|namespace|LoRA)|los(?:e|es|t).*(?:state|accepted|updates)|dangling|use.after.free|错误输出|串扰|错写", text, re.I):
        return "P0", "报告涉及输出、缓存数据或请求隔离的正确性，优先核对状态与所有权。"
    if re.search(r"crash|deadlock|hang\b|wedg|out.of.memory|\bOOM\b|leak|illegal.memory|assert|thread dies|stall|exception|timeout|卡死|泄漏|崩溃", text, re.I):
        return "P1", "报告涉及崩溃、等待、内存耗尽或释放问题，可能影响服务可用性。"
    if re.search(r"perf|latency|throughput|speed|redundant|overhead|dedup|miss|hit.rate|evict|optimi|capacity|budget|缓存命中|吞吐|延迟", text, re.I):
        return "P2", "主要影响性能、缓存命中或容量利用；需以同配置测量判断收益。"
    return "P3", "暂未发现明确的正确性或可用性故障证据，按能力与维护工作跟进。"


def confidence(body: str) -> dict:
    meaningful = clean_body(body)
    meaningful = re.sub(r"(?im)^\s*-\s*\[[ xX]\].*$", "", meaningful)
    signals = []
    if re.search(r"```[\s\S]{40,}?```|\bcurl\s|\bpython(?:3)?\s|steps to reproduce|reproduction script", meaningful, re.I):
        signals.append("提供复现命令、脚本或可执行步骤")
    if re.search(r"\b(?:H100|H200|B200|B300|GB300|A100|A800|L40S?|RTX|SM\d+|CUDA|ROCm|v0\.\d+|commit|PyTorch|environment)\b", meaningful, re.I):
        signals.append("说明版本、硬件或运行环境")
    if re.search(r"Traceback|AssertionError|RuntimeError|before.{0,100}after|\d+/\d+|\d+ (?:passed|failed)|expected.{0,80}actual|actual.{0,80}expected", meaningful, re.I | re.S):
        signals.append("包含失败输出、观测结果或前后对照")
    negative = bool(re.search(r"not (?:yet )?reproduced|unable to reproduce|could not reproduce|unconfirmed|hypothesis only|not a serving bug", meaningful, re.I))
    level = "high" if len(signals) == 3 and not negative else "medium" if len(signals) >= 2 else "low"
    if negative:
        level = "low"
        signals.append("正文明确包含尚未复现或适用范围限制")
    return {"level": level, "label": {"high": "较高", "medium": "中等", "low": "待核验"}[level], "evidence": signals or ["尚缺足够的环境、步骤和观测证据"], "meaning": "按报告证据完整度自动评估，不是故障概率、根因确认或本地复现结论。"}


def normalize(item: dict) -> dict:
    # Accept REST results and the local GraphQL bootstrap export.
    if "created_at" in item:
        return {"number": item["number"], "title": item["title"], "body": item.get("body") or "", "state": item["state"].lower(), "url": item["html_url"], "created_at": item["created_at"], "updated_at": item["updated_at"], "is_pr": "pull_request" in item, "draft": item.get("draft", False), "author": item.get("user", {}).get("login", ""), "labels": [v["name"] for v in item.get("labels", [])], "comments": item.get("comments", 0)}
    raise ValueError("Expected a GitHub REST issue or pull request")


def to_entry(item: dict, editorial: dict) -> dict | None:
    if item["state"] != "open":
        return None
    modules = classify_modules(item["title"], item["body"])
    if not modules:
        return None
    source_kind = "pr" if item["is_pr"] else "issue"
    kind = "rfc" if re.search(r"\bRFC\b(?!\s*#\d)|request for comments", item["title"], re.I) else source_kind
    notes = editorial.get(str(item["number"]))
    p, reason = priority(item["title"], item["body"], kind)
    description = prose(item["body"])
    if notes:
        p = notes["priority"]
        reason = "人工整理的待办优先级；请结合当前源文与后续讨论复核。"
    summary = (notes or {}).get("summary") or (description[0][:520] if description else "作者暂未提供详细正文，请在 GitHub 查看讨论与后续补充。")
    return {**item, "id": REPO + "#" + str(item["number"]), "repository": REPO, "body": clean_body(item["body"]), "modules": modules, "kind": kind, "source_kind": source_kind, "priority": p, "priority_reason": reason, "summary": summary, "explanation": description[:6], "editorial": notes, "confidence": confidence(item["body"]) if not item["is_pr"] else None, "related": sorted({int(n) for n in re.findall(r"(?<![\w/])#(\d{4,6})\b", clean_body(item["body"])) if int(n) != item["number"]})[:15]}


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


class GitHub:
    def __init__(self, use_gh: bool = False):
        self.use_gh = use_gh
        self.calls = 0

    def get(self, endpoint: str) -> list | dict:
        self.calls += 1
        if self.use_gh:
            result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, check=True)
            return json.loads(result.stdout)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "SGLang-Cache-Observatory", "X-GitHub-Api-Version": "2022-11-28"}
        if token := os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = "Bearer " + token
        request = urllib.request.Request("https://api.github.com/" + endpoint, headers=headers)
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=45) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                if error.code in (403, 429) and (error.headers.get("X-RateLimit-Remaining") == "0" or error.headers.get("Retry-After")):
                    raise RuntimeError("GitHub rate limit reached; retained the last complete snapshot") from error
                if error.code not in (500, 502, 503, 504) or attempt == 3:
                    raise
            except (urllib.error.URLError, TimeoutError):
                if attempt == 3:
                    raise
            time.sleep(2 ** attempt)
        raise RuntimeError("GitHub request did not complete")

    def issues(self, since: str | None = None):
        page = 1
        while True:
            params = {"state": "all" if since else "open", "sort": "updated" if since else "created", "direction": "desc", "per_page": 100, "page": page}
            if since:
                params["since"] = since
            batch = self.get(f"repos/{REPO}/issues?" + urllib.parse.urlencode(params))
            if not isinstance(batch, list):
                raise ValueError("Expected a complete GitHub issue page")
            if not batch:
                return
            yield batch
            if len(batch) < 100:
                return
            page += 1
            if page > 1000:
                raise RuntimeError("Unexpected repository size; refusing a partial snapshot")
