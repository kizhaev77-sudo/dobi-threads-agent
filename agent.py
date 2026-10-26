#!/usr/bin/env python3
"""
Доби-агент для Threads (официальный Threads API, без браузера).

Команды:
  python agent.py status          — проверить токен, лимиты, разрешения
  python agent.py post            — опубликовать следующий пост из posts.json (если пришло время)
  python agent.py replies         — ответить на новые комментарии под своими постами
  python agent.py promo           — найти ветки саморекламы и ответить (нужен одобренный threads_keyword_search)
  python agent.py run             — всё по очереди: post + replies + promo
  python agent.py refresh-token   — продлить токен ещё на 60 дней (печатает новый токен)

Переменные окружения:
  THREADS_TOKEN       — долгий токен Threads (обязательно)
  ANTHROPIC_API_KEY   — необязательно: тогда ответы на комментарии пишет Claude (Haiku)
  DRY_RUN=1           — ничего не публиковать, только показать, что было бы сделано
"""
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
MSK = timezone(timedelta(hours=3))

DRY_RUN = os.environ.get("DRY_RUN", "") not in ("", "0", "false")


# ---------------------------------------------------------------- утилиты

def load_json(name, default):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(name, data):
    path = os.path.join(HERE, name)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def log(*a):
    print(datetime.now(MSK).strftime("%d.%m %H:%M"), *a, flush=True)


def now_msk():
    return datetime.now(MSK)


CFG = load_json("config.json", {})
API_ROOT = "https://" + CFG.get("api_host", "graph.threads.com")
API = API_ROOT + "/v1.0"
STATE = load_json("state.json", {})
STATE.setdefault("replied_comments", [])     # id комментариев, на которые уже ответили
STATE.setdefault("promo_threads", [])        # id веток саморекламы, где уже отвечали
STATE.setdefault("promo_authors_by_day", {}) # день -> авторы
STATE.setdefault("posted", [])               # id постов из posts.json, уже опубликованы
STATE.setdefault("last_post_at", None)
STATE.setdefault("text_index", 0)
STATE.setdefault("skipped_for_kir", [])      # комментарии, которые агент сознательно не трогал


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body


def http(method, url, params=None, data=None, timeout=60):
    params = dict(params or {})
    if "access_token" not in params:
        params["access_token"] = token()
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    body = None
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(full, data=body, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace"))


def token():
    t = os.environ.get("THREADS_TOKEN", "").strip()
    if not t:
        sys.exit("Нет THREADS_TOKEN. См. ИНСТРУКЦИЯ.md, шаг 3.")
    return t


def get(path, **params):
    return http("GET", f"{API}/{path}", params)


def paged(path, limit_pages=3, **params):
    items, after = [], None
    for _ in range(limit_pages):
        if after:
            params["after"] = after
        res = get(path, **params)
        items += res.get("data", [])
        after = res.get("paging", {}).get("cursors", {}).get("after")
        if not after or not res.get("data"):
            break
    return items


# ---------------------------------------------------------------- публикация

def publish(text=None, reply_to=None, image_url=None, video_url=None):
    """Создать контейнер и опубликовать. Возвращает id поста или None в DRY_RUN."""
    if text and len(text) > 500:
        raise ValueError(f"Текст длиннее 500 знаков ({len(text)}): {text[:60]}…")
    data = {"text": text or ""}
    if video_url:
        data.update(media_type="VIDEO", video_url=video_url)
    elif image_url:
        data.update(media_type="IMAGE", image_url=image_url)
    else:
        data["media_type"] = "TEXT"
    if reply_to:
        data["reply_to_id"] = reply_to
    if DRY_RUN:
        log("[DRY_RUN] опубликовал бы:", json.dumps(data, ensure_ascii=False)[:400])
        return None
    c = http("POST", f"{API}/me/threads", data=data)
    cid = c["id"]
    # видео обрабатывается дольше — ждём статуса FINISHED
    wait = 30 if not video_url else 10
    for _ in range(30):
        time.sleep(wait)
        if not video_url:
            break
        st = get(cid, fields="status,error_message")
        if st.get("status") == "FINISHED":
            break
        if st.get("status") == "ERROR":
            raise ApiError(0, f"видео не обработалось: {st}")
    p = http("POST", f"{API}/me/threads_publish", data={"creation_id": cid})
    return p.get("id")


# ---------------------------------------------------------------- тексты

def phrase():
    return CFG.get("kir_phrase", "Доби - ваш друг,собеседник и помощник.")


def link(campaign):
    base = CFG.get("site", "iimagazin.ru")
    return f"{base}/?utm_source=threads&utm_medium=reply&utm_campaign={campaign}"


def promo_text(author):
    texts = CFG["promo_texts"] if CFG.get("promo_video_url") else CFG.get("promo_texts_no_video", CFG["promo_texts"])
    i = STATE["text_index"] % len(texts)
    STATE["text_index"] += 1
    camp = re.sub(r"[^\w]", "_", f"{CFG.get('promo_campaign', 'osago')}_{author}")[:60]
    return texts[i].replace("LINK", link(camp))


def bad_thread(text):
    t = text.lower()
    for w in CFG.get("skip_words", []):
        if w.lower() in t:
            return w
    return None


# ---------------------------------------------------------------- 1. свои посты

def cmd_post():
    posts = load_json("posts.json", [])
    todo = [p for p in posts if p["id"] not in STATE["posted"]]
    if not todo:
        log("posts.json: всё опубликовано — добавьте новые посты.")
        return
    gap_h = CFG.get("hours_between_posts", 6)
    last = STATE.get("last_post_at")
    if last and now_msk() - datetime.fromisoformat(last) < timedelta(hours=gap_h):
        log(f"Свой пост: ещё рано (интервал {gap_h} ч).")
        return
    p = todo[0]
    main = publish(text=p["text"], image_url=p.get("image_url"), video_url=p.get("video_url"))
    log("Опубликован пост", p["id"], main or "")
    if p.get("second"):
        time.sleep(5)
        second = p["second"].replace("LINK", f"{CFG.get('site','iimagazin.ru')}/?utm_source=threads&utm_medium=post&utm_campaign=post_{p['id']}")
        if main or DRY_RUN:
            publish(text=second, reply_to=main)
            log("  + вторая часть со ссылкой")
    if not DRY_RUN:
        STATE["posted"].append(p["id"])
        STATE["last_post_at"] = now_msk().isoformat()


# ---------------------------------------------------------------- 2. ответы на комментарии

ASK_HOW = re.compile(r"(как (вы )?(это )?(сделал|делал|создал)|как? сделан|какой (ии|нейросет)|на ч[её]м сделан|какая нейросеть|в какой программе)", re.I)
NEGATIVE = re.compile(r"(спам|развод|лохотрон|отстань|бот\b|реклама задолбала|иди ты)", re.I)


def claude_reply(comment, post_text):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    system = CFG["claude_system_prompt"]
    body = json.dumps({
        "model": CFG.get("claude_model", "claude-haiku-4-5"),
        "max_tokens": 300,
        "system": system,
        "messages": [{"role": "user", "content":
                      f"Наш пост:\n{post_text[:800]}\n\nКомментарий пользователя:\n{comment}\n\n"
                      "Напиши ответ (до 300 знаков). Если отвечать не нужно — напиши ровно SKIP."}],
    }).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, method="POST",
                                 headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                                          "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.loads(r.read().decode())
        txt = "".join(b.get("text", "") for b in out.get("content", [])).strip()
        return None if not txt or txt.upper().startswith("SKIP") else txt[:480]
    except Exception as e:
        log("Claude недоступен:", e)
        return None


def template_reply(comment):
    c = comment.lower()
    for rule in CFG.get("reply_templates", []):
        if re.search(rule["if"], c, re.I):
            return random.choice(rule["say"])
    return None


def cmd_replies():
    me = get("me", fields="id,username")
    my_name = me.get("username")
    posts = paged("me/threads", limit_pages=1, fields="id,text,timestamp", limit=CFG.get("check_last_posts", 15))
    answered = 0
    max_answers = CFG.get("max_comment_replies_per_run", 10)
    for p in posts:
        try:
            conv = paged(f"{p['id']}/conversation", limit_pages=3,
                         fields="id,text,username,timestamp,is_reply_owned_by_me,replied_to", limit=50)
        except ApiError as e:
            log("Не прочитал комментарии к", p["id"], e)
            continue
        mine_parents = {(r.get("replied_to") or {}).get("id") for r in conv
                        if r.get("is_reply_owned_by_me") or r.get("username") == my_name}
        for r in conv:
            rid = r["id"]
            if r.get("is_reply_owned_by_me") or r.get("username") == my_name:
                continue
            if rid in STATE["replied_comments"] or rid in mine_parents:
                continue
            text = (r.get("text") or "").strip()
            if not text:
                continue
            if ASK_HOW.search(text) or NEGATIVE.search(text):
                # Правило Кира: вопросы «как сделано / какой ИИ» и негатив — не отвечаем, Кир решает сам
                if rid not in STATE["skipped_for_kir"]:
                    STATE["skipped_for_kir"].append(rid)
                    log(f"Оставил Киру: @{r.get('username')}: {text[:80]}")
                continue
            ans = claude_reply(text, p.get("text") or "") or template_reply(text)
            if not ans:
                continue
            publish(text=ans, reply_to=rid)
            log(f"Ответил @{r.get('username')}: {ans[:80]}")
            if not DRY_RUN:
                STATE["replied_comments"].append(rid)
            answered += 1
            if answered >= max_answers:
                return
            time.sleep(random.randint(20, 60))
    log(f"Ответов на комментарии: {answered}")


# ---------------------------------------------------------------- 3. ветки саморекламы

def cmd_promo():
    today = now_msk().strftime("%Y-%m-%d")
    authors_today = STATE["promo_authors_by_day"].setdefault(today, [])
    limit = CFG.get("max_promo_replies_per_run", 6)
    day_limit = CFG.get("max_promo_replies_per_day", 30)
    if len(authors_today) >= day_limit:
        log("Самореклама: дневной лимит достигнут.")
        return
    done = 0
    for q in CFG.get("promo_queries", []):
        try:
            res = get("keyword_search", q=q, search_type="RECENT", limit=25,
                      fields="id,text,username,timestamp,permalink,is_reply")
        except ApiError as e:
            if e.status in (400, 403):
                log("Поиск чужих веток пока недоступен (нужно одобрение threads_keyword_search в Meta).")
                return
            raise
        for t in res.get("data", []):
            if done >= limit or len(authors_today) >= day_limit:
                return
            tid, au = t["id"], t.get("username") or ""
            text = t.get("text") or ""
            if t.get("is_reply") or tid in STATE["promo_threads"] or au in authors_today:
                continue
            if not re.search(r"(ветк|самореклам|коснулся)", text, re.I):
                continue
            why = bad_thread(text)
            if why:
                log(f"Пропуск @{au} ({why})")
                STATE["promo_threads"].append(tid)
                continue
            ts = datetime.fromisoformat(t["timestamp"].replace("+0000", "+00:00"))
            if datetime.now(timezone.utc) - ts > timedelta(hours=CFG.get("promo_max_age_hours", 24)):
                continue
            msg = promo_text(au)
            # Ответ с роликом (ролик должен лежать по публичной ссылке)
            publish(text=msg, reply_to=tid, video_url=CFG.get("promo_video_url") or None)
            log(f"Самореклама: ответил в ветке @{au} {t.get('permalink','')}")
            if not DRY_RUN:
                STATE["promo_threads"].append(tid)
                authors_today.append(au)
            done += 1
            time.sleep(random.randint(60, 150))
    log(f"Самореклама: ответов {done}")


# ---------------------------------------------------------------- служебные

def cmd_status():
    me = get("me", fields="id,username,name")
    log("Аккаунт:", me.get("username"), me.get("id"))
    try:
        lim = get("me/threads_publishing_limit", fields="quota_usage,config,reply_quota_usage,reply_config")
        log("Лимиты публикаций:", json.dumps(lim.get("data", lim), ensure_ascii=False))
    except ApiError as e:
        log("Лимиты не прочитал:", e)
    try:
        get("keyword_search", q="самореклама", search_type="RECENT", limit=1)
        log("Поиск: работает (если находит только свои посты — одобрения ещё нет).")
    except ApiError as e:
        log("Поиск недоступен:", e.status)


def cmd_refresh():
    res = http("GET", f"{API_ROOT}/refresh_access_token",
               {"grant_type": "th_refresh_token", "access_token": token()})
    print(res.get("access_token"))
    log("Токен продлён на", int(res.get("expires_in", 0)) // 86400, "дн.")


def quiet_hours():
    h = now_msk().hour
    start, end = CFG.get("quiet_hours_msk", [0, 7])
    return start <= h < end


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd in ("post", "replies", "promo", "run") and quiet_hours():
        log("Ночь по Москве — ничего не делаю.")
        return
    try:
        if cmd == "status":
            cmd_status()
        elif cmd == "refresh-token":
            cmd_refresh()
        elif cmd == "post":
            cmd_post()
        elif cmd == "replies":
            cmd_replies()
        elif cmd == "promo":
            cmd_promo()
        elif cmd == "run":
            for f in (cmd_post, cmd_replies, cmd_promo):
                try:
                    f()
                except ApiError as e:
                    log(f"{f.__name__}: ошибка API — {e}")
        else:
            sys.exit(__doc__)
    finally:
        if not DRY_RUN:
            # обрезаем историю, чтобы файл не рос бесконечно
            for k in ("replied_comments", "promo_threads", "skipped_for_kir"):
                STATE[k] = STATE[k][-3000:]
            days = sorted(STATE["promo_authors_by_day"])[-7:]
            STATE["promo_authors_by_day"] = {d: STATE["promo_authors_by_day"][d] for d in days}
            save_json("state.json", STATE)


if __name__ == "__main__":
    main()
