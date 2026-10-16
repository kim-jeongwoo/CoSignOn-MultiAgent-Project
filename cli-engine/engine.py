#!/usr/bin/env python3
# engine.py — 새 알고리즘(v2)을 시험하는 작은 채팅방 엔진. 기존 main.py 등과 완전히 독립된 한 파일짜리 버전.
# AI 3명(Claude, GPT, Gemini)이 고정 순서 없이 공유 폴더의 chat.md/summary.md/draft.md를 직접 읽고 토의한다.
# 실행: python3 engine.py  (표준 라이브러리만 사용)

import json
import os
import random
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

# ───────────────────────── 설정 ─────────────────────────
NAMES = ["Claude", "GPT", "Gemini"]
HUMAN = "사람"
ENGINE = "엔진"
RECIPIENTS = NAMES + ["전체", HUMAN]
TIMEOUT_SEC = 300
HUMAN_EVERY_POSTS = 30      # 글이 30개를 넘을 때마다 사람에게 묻는다
MAX_SUMMARY_REJECTS = 3     # 같은 주제 요약안이 3회 반대되면 사람에게 묻는다
WORKDIR_ROOT = Path.home() / ".chatroom_workdir"

# 구독 계정으로만 실행되도록 자식 프로세스 환경에서 API 키를 뺀다.
API_KEY_VARS = ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"]

# 10/15 테스트로 확인된 호출 옵션
COMMANDS = {
    "Claude": lambda p: ["claude", "-p", p, "--model", "claude-opus-5-5", "--permission-mode", "acceptEdits"],
    "GPT": lambda p: ["codex", "exec", "--skip-git-repo-check", "--sandbox", "workspace-write", p],
    "Gemini": lambda p: ["agy", "-p", p, "--dangerously-skip-permissions"],
}

# ───────────────────────── 프롬프트 ─────────────────────────
BASE_PROMPT = (
    "너는 {name}이다. 팀원은 Claude, GPT, Gemini, 그리고 사람. "
    "현재 폴더의 summary.md를 읽고, chat.md에서 {n}번 이후 글만 읽고, 필요하면 draft.md를 읽어라. "
    "{n}번 이전 글이 더 필요하면 '사람'에게 요청하라.\n"
    "할 말이 있는 경우는 셋뿐이다: 나에게 온 질문·지목 / 반대·수정 / 결론에 영향을 주는 새 정보. "
    "아니면 PASS만 출력하라.\n"
    "산출물을 고칠 때는 draft.md를 직접 수정하고 글에 무엇을 고쳤는지 적어라.\n"
    "출력 형식: 첫 줄은 PASS 또는 'TO: Claude|GPT|Gemini|전체|사람' 중 하나. 둘째 줄부터 본문.\n"
    "사람에게 할 말은 반드시 'TO: 사람'으로 써라.\n"
    "요약안을 낼 때는 본문 첫 줄을 [요약안]으로 시작하라(엔진이 '요약안#글번호'를 붙여 기록한다). "
    "요약안에 답할 때는 그 번호를 넣어 [동의 #번호] 또는 [반대 #번호: 이유]로 시작하라. "
    "요약안을 고치려면 반드시 새 [요약안] 글로 올려라."
)

# 1단계(독립 의견)에서만 BASE_PROMPT 뒤에 붙인다
FIRST_OPINION_NOTE = "\n이번에는 draft.md를 수정하지 말고, [요약안]도 쓰지 말고, 너의 의견만 글로 써라."

REJUDGE_PROMPT = (
    "너는 {name}이다. 현재 폴더의 chat.md에 글을 올리려는 사이 다른 글이 올라왔다.\n"
    "네가 쓰려던 글:\nTO: {to}\n{body}\n\n"
    "그 사이 새 글: chat.md의 {a}~{b}번 (읽어 보라)\n"
    "SEND, EDIT(고친 글), CANCEL 중 하나로 답하라.\n"
    "출력 형식: 첫 줄은 SEND 또는 EDIT 또는 CANCEL. "
    "EDIT이면 둘째 줄부터 고친 글을 원래 형식(첫 줄 'TO: 받는이', 다음 줄부터 본문)으로 써라."
)

CONCLUSION_PROMPT = "너는 {name}이다. 현재 폴더의 chat.md와 summary.md를 읽고 한 줄 결론만 출력하라."

# ───────────────────────── 응답 해석 ─────────────────────────
TO_RE = re.compile(r"^TO\s*[:：]\s*(Claude|GPT|Gemini|전체|사람)", re.IGNORECASE)
PASS_RE = re.compile(r"^PASS\b", re.IGNORECASE)
DECISION_RE = re.compile(r"^(SEND|EDIT|CANCEL)\b", re.IGNORECASE)
SUMMARY_RE = re.compile(r"^\s*\[(요약안|동의|반대)")
PROPOSAL_TAG_RE = re.compile(r"^\[요약안[^\]]*\]")          # [요약안], [요약안#3] 등
VOTE_RE = re.compile(r"^\[(동의|반대)\s*#\s*(\d+)")          # [동의 #6], [반대 #6: 이유]


def clean_line(line):
    """마크다운 강조(**, `, #, >)를 벗긴 한 줄."""
    return line.strip().strip("*`#> ").strip()


def canonical(name):
    for r in RECIPIENTS:
        if r.lower() == name.strip().lower():
            return r
    return "전체"


def parse_reply(text):
    """AI 답 → (PASS인가, 받는이, 본문, 형식 준수 여부).
    앞에 잡담이 붙어도 처음 나오는 PASS 또는 TO: 줄을 찾는다. 둘 다 없으면 '전체'에게 보낸 글로 본다."""
    lines = (text or "").strip().splitlines()
    for i, raw in enumerate(lines):
        s = clean_line(raw)
        if not s:
            continue
        if PASS_RE.match(s):
            return True, None, "", True
        m = TO_RE.match(s)
        if m:
            rest = s[m.end():].strip()
            body = "\n".join(([rest] if rest else []) + lines[i + 1:]).strip()
            if not body:
                return True, None, "", False
            return False, canonical(m.group(1)), body, True
    body = "\n".join(lines).strip()
    if not body:
        return True, None, "", False
    return False, "전체", body, False


# ───────────────────────── 엔진 ─────────────────────────
class Engine:
    def __init__(self):
        self.chat_lock = threading.Lock()       # 세마포: chat.md 쓰기는 반드시 이 안에서
        self.cond = threading.Condition()       # AI 상태(생각 중/마지막 읽은 번호) 보호 + 깨우기 신호
        self.print_lock = threading.Lock()
        self.log_lock = threading.Lock()

        self.posts = []                          # [{num, time, from, to, body}]
        self.last_read = {n: 0 for n in NAMES}
        self.thinking = {n: False for n in NAMES}
        self.stop = False

        self.human_pending = False
        self.human_reasons = []
        self.next_human_at = HUMAN_EVERY_POSTS

        self.proposal = None                     # 진행 중인 요약안 {num, author, text, agree}
        self.reject_streak = 0
        self.summary_count = 0

        self.records = []                        # log.jsonl과 같은 내용(집계용)

        self.env = dict(os.environ)
        removed = [k for k in API_KEY_VARS if self.env.pop(k, None) is not None]
        if removed:
            print(f"※ 구독 사용을 위해 환경변수 제거: {', '.join(removed)}")

    # ── 출력·기록 ──
    def say(self, msg):
        with self.print_lock:
            print(msg, flush=True)

    def log_call(self, name, kind, secs, is_pass, ok, **extra):
        rec = {"시각": datetime.now().isoformat(timespec="seconds"), "이름": name, "종류": kind,
               "걸린시간초": round(secs, 1), "PASS": is_pass, "성공": ok}
        rec.update({k: v for k, v in extra.items() if v is not None})
        with self.log_lock:
            self.records.append(rec)
            with open(self.dir / "log.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ── CLI 호출 ──
    def run_cli(self, name, prompt):
        """(stdout, 오류 문자열 또는 None, 걸린 초). 실패면 stdout은 빈 문자열."""
        start = time.time()
        try:
            # 새 프로세스 그룹으로 실행 → 타임아웃 때 CLI가 남긴 자식 프로세스까지 한꺼번에 종료할 수 있다
            p = subprocess.Popen(COMMANDS[name](prompt), cwd=self.dir, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                 env=self.env, start_new_session=True)
            try:
                stdout, stderr = p.communicate(timeout=TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                self._kill_group(p)
                raise
            out = stdout.strip()
            if p.returncode != 0:
                err = f"종료코드 {p.returncode}: {stderr.strip()[-200:]}"
                out = ""
            elif not out:
                err = "빈 응답"
            else:
                err = None
        except subprocess.TimeoutExpired:
            out, err = "", f"타임아웃 {TIMEOUT_SEC}초"
        except FileNotFoundError:
            out, err = "", "명령을 찾을 수 없음"
        except Exception as e:
            out, err = "", f"{type(e).__name__}: {e}"
        if err:
            self.say(f"  ⚠ {name} 호출 실패({err}) → PASS로 처리")
        return out, err, time.time() - start

    @staticmethod
    def _kill_group(p):
        """프로세스 그룹 전체를 SIGKILL로 끝내고 남은 출력을 회수한다(좀비 프로세스도 정리됨)."""
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            pass    # 그룹을 스스로 빠져나간 손자 프로세스가 파이프를 쥔 경우: 더 기다리지 않는다

    # ── chat.md 붙이기 (반드시 chat_lock을 잡은 상태에서 호출) ──
    def _append(self, sender, to, body):
        with self.cond:
            num, body = len(self.posts) + 1, body.strip()
            if sender in NAMES and PROPOSAL_TAG_RE.match(body):   # [요약안] → [요약안#글번호]
                body = PROPOSAL_TAG_RE.sub(f"[요약안#{num}]", body, count=1)
            post = {"num": num, "time": datetime.now().strftime("%H:%M:%S"),
                    "from": sender, "to": to, "body": body}
            self.posts.append(post)
            line = f"[{post['num']}][{post['time']}][{sender}→{to}] {post['body']}"
            with open(self.dir / "chat.md", "a", encoding="utf-8") as f:
                f.write(line + "\n\n")
            self.say(line)
            self._after_post(post)
            self.cond.notify_all()
        return post

    def _after_post(self, post):
        """요약 합의 처리 + 사람 호출 조건 확인. _append 안에서만 불린다."""
        sender, first = post["from"], post["body"].lstrip()
        if sender in NAMES:
            if PROPOSAL_TAG_RE.match(first):
                if self.proposal:
                    self.say(f"  (요약안 #{self.proposal['num']}은 새 요약안 #{post['num']}로 대체됨)")
                self.proposal = {"num": post["num"], "author": sender,
                                 "text": PROPOSAL_TAG_RE.sub("", first, count=1).strip(), "agree": set()}
            else:
                v = VOTE_RE.match(first)
                if v:
                    self._handle_vote(sender, v.group(1), int(v.group(2)))
                elif first.startswith("[동의") or first.startswith("[반대"):
                    self.say(f"  ({sender}의 #{post['num']}: 요약안 번호가 없어 집계하지 않음)")
            if post["to"] == HUMAN:
                self._request_human(f"{sender}가 사람에게 글을 보냄 (#{post['num']})")
        if len(self.posts) > self.next_human_at:
            self.next_human_at += HUMAN_EVERY_POSTS
            self._request_human(f"글이 {len(self.posts) - 1}개를 넘음")

    def _handle_vote(self, sender, kind, target):
        """[동의 #n]/[반대 #n]을 요약안 번호 n으로 집계. 진행 중인 요약안이 아니면(대체·확정·반대됨) 무시."""
        p = self.proposal
        if not p or p["num"] != target:
            self.say(f"  ({sender}의 {kind} #{target}: 진행 중인 요약안이 아니어서 집계하지 않음)")
            return
        if sender == p["author"]:
            return
        if kind == "동의":
            p["agree"].add(sender)
            if p["agree"] >= set(NAMES) - {p["author"]}:
                self._confirm_summary()
        else:
            self.reject_streak += 1
            self.say(f"  (요약안 #{p['num']} 반대됨: 연속 {self.reject_streak}회)")
            self.proposal = None
            if self.reject_streak >= MAX_SUMMARY_REJECTS:
                self.reject_streak = 0
                self._request_human(f"요약안이 {MAX_SUMMARY_REJECTS}회 반대됨")

    def _confirm_summary(self):
        p = self.proposal
        self.proposal = None
        self.reject_streak = 0
        self.summary_count += 1
        others = ", ".join(sorted(p["agree"]))
        with open(self.dir / "summary.md", "a", encoding="utf-8") as f:
            f.write(f"\n## 요약 {self.summary_count} (요약안 #{p['num']}, 작성 {p['author']}, 동의 {others})\n\n"
                    f"{p['text']}\n")
        self._append(ENGINE, "전체", f"요약 확정: #{p['num']}번 요약안을 summary.md에 저장했습니다.")

    def _request_human(self, reason):
        if self.stop:
            return
        self.human_pending = True
        self.human_reasons.append(reason)

    # ── AI 상태 ──
    def has_new(self, name):
        """자기 글을 뺀 새 글이 있는가 (cond 안에서 호출)."""
        return any(p["from"] != name for p in self.posts[self.last_read[name]:])

    def all_idle(self):
        return not any(self.thinking.values()) and not any(self.has_new(n) for n in NAMES)

    def mark_read(self, name, upto):
        with self.cond:
            self.last_read[name] = max(self.last_read[name], upto)
            self.cond.notify_all()

    # ── 1단계: 독립 의견 ──
    def first_opinions(self):
        self.say("\n=== 1단계: 독립 의견 (세 AI 동시 호출) ===")
        results = {}

        def job(name):
            results[name] = self.run_cli(name, BASE_PROMPT.format(name=name, n=0) + FIRST_OPINION_NOTE)

        threads = [threading.Thread(target=job, args=(n,), daemon=True) for n in NAMES]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 셋 다 끝난 뒤 한꺼번에 붙인다 (서로의 첫 의견을 못 보게)
        upto = len(self.posts)
        with self.chat_lock:
            for name in NAMES:
                out, err, secs = results[name]
                is_pass, to, body, fmt_ok = parse_reply(out)
                self.log_call(name, "첫의견", secs, is_pass, err is None, 오류=err,
                              형식준수=None if is_pass else fmt_ok)
                if not is_pass:
                    self._append(name, to, body)
        for name in NAMES:
            self.mark_read(name, upto)

    # ── 2단계: 토의 ──
    def worker(self, name):
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.stop or (not self.human_pending and self.has_new(name)))
                if self.stop:
                    return
                self.thinking[name] = True
                n, upto = self.last_read[name], len(self.posts)
            try:
                self.discuss_turn(name, n, upto)
            finally:
                with self.cond:
                    self.thinking[name] = False
                    self.cond.notify_all()

    def discuss_turn(self, name, n, upto):
        self.say(f"  ({name} 생각 중… {n}번 이후 글 읽기)")
        out, err, secs = self.run_cli(name, BASE_PROMPT.format(name=name, n=n))
        is_pass, to, body, fmt_ok = parse_reply(out)
        kind = "요약" if not is_pass and SUMMARY_RE.match(body) else "토의"
        self.log_call(name, kind, secs, is_pass, err is None, 오류=err, 형식준수=None if is_pass else fmt_ok)
        if is_pass:
            self.say(f"  ({name} PASS)")
            self.mark_read(name, upto)
            return

        with self.chat_lock:
            latest = len(self.posts)
            if latest == upto:                   # 그 사이 새 글 없음 → 바로 붙인다
                self._append(name, to, body)
                self.mark_read(name, upto)
                return
        # 그 사이 새 글이 있음 → 락을 푼 상태에서 재판단
        decision, to, body = self.rejudge(name, to, body, upto + 1, latest)
        if decision != "CANCEL":
            with self.chat_lock:
                self._append(name, to, body)
        self.mark_read(name, latest)

    def rejudge(self, name, to, body, a, b):
        self.say(f"  ({name}: 쓰는 사이 새 글 {a}~{b}번 → 재판단)")
        out, err, secs = self.run_cli(name, REJUDGE_PROMPT.format(name=name, to=to, body=body, a=a, b=b))
        decision, note = "SEND", None
        lines = out.strip().splitlines()
        first = clean_line(lines[0]) if lines else ""
        m = DECISION_RE.match(first)
        if m:
            decision = m.group(1).upper()
            if decision == "EDIT":
                rest = first[m.end():].strip(" :：-")
                edited = "\n".join(([rest] if rest else []) + lines[1:]).strip()
                p, t, b2, fmt_ok = parse_reply(edited)
                if p or not b2:
                    decision, note = "CANCEL", "EDIT인데 고친 글 없음"
                else:
                    to, body = (t if fmt_ok else to), b2
        elif out:
            p, t, b2, fmt_ok = parse_reply(out)
            if fmt_ok and not p:                 # SEND/EDIT 없이 고친 글만 낸 경우 → EDIT로 본다
                decision, to, body, note = "EDIT", t, b2, "결정어 없이 고친 글만 출력"
            else:
                note = "재판단 불분명→SEND"      # 형식을 알 수 없음 → 원래 쓰려던 글 그대로
        else:
            note = "재판단 불분명→SEND"          # 빈 응답·타임아웃·실패 → 원래 쓰려던 글 그대로
        self.log_call(name, "재판단", secs, decision == "CANCEL", err is None and note is None,
                      결과=decision, 오류=err, 비고=note)
        self.say(f"  ({name} 재판단 결과: {decision})")
        return decision, to, body

    # ── 사람 ──
    def main_loop(self):
        """모두 쉬거나 사람이 '종료'할 때까지 기다린다. 끝난 이유를 돌려준다."""
        while True:
            with self.cond:
                self.cond.wait_for(lambda: self.human_pending or self.all_idle())
                if not self.human_pending:
                    return "세 AI 모두 마지막 글에 PASS"
                # 생각 중인 AI가 끝날 때까지 기다린 뒤 묻는다 (새 호출은 human_pending 동안 시작 안 됨)
                self.cond.wait_for(lambda: not any(self.thinking.values()))
                reasons = list(self.human_reasons)
                self.human_reasons.clear()
            answer = self.ask_human(reasons)
            with self.cond:
                self.human_pending = bool(self.human_reasons)
                self.cond.notify_all()
            if answer == "종료":
                return "사람이 종료"

    def ask_human(self, reasons):
        self.say("\n──── 사람 차례 ────")
        for r in reasons:
            self.say(f"  이유: {r}")
        self.say(f"  (세션 폴더: {self.dir})")
        content = input("사람 입력 (엔터=그냥 계속, '종료'=끝): ").strip()
        if content == "종료":
            return "종료"
        if not content:
            return None
        to = input("받는이 (Claude/GPT/Gemini/전체, 엔터=전체): ").strip()
        to = canonical(to) if to else "전체"
        if to == HUMAN:
            to = "전체"
        with self.chat_lock:
            self._append(HUMAN, to, content)
        return None

    # ── 4단계: 종료 ──
    def conclude(self):
        self.say("\n=== 결론 ===")
        # 마지막으로 글을 쓴 AI가 아닌 AI 중 무작위 → 실패하면 다른 AI (마지막 작성자는 맨 끝 예비)
        last = next((p["from"] for p in reversed(self.posts) if p["from"] in NAMES), None)
        order = [n for n in NAMES if n != last]
        random.shuffle(order)
        if last:
            order.append(last)
        for name in order:
            out, err, secs = self.run_cli(name, CONCLUSION_PROMPT.format(name=name))
            line = next((clean_line(l) for l in out.splitlines() if clean_line(l)), "")
            self.log_call(name, "결론", secs, False, bool(line), 오류=err)
            if line:
                with self.chat_lock:
                    self._append(name, "전체", f"[결론] {line}")
                return
        self.say("  결론을 받지 못했습니다.")

    def print_stats(self):
        print("\n=== 집계 ===")
        print(f"{'이름':<8}{'호출':>6}{'PASS':>6}{'실패':>6}{'재판단':>8}  SEND/EDIT/CANCEL")
        for name in NAMES:
            recs = [r for r in self.records if r["이름"] == name]
            rj = [r for r in recs if r["종류"] == "재판단"]
            passes = sum(1 for r in recs if r["PASS"] and r["종류"] != "재판단")
            fails = sum(1 for r in recs if not r["성공"])
            cnt = {d: sum(1 for r in rj if r.get("결과") == d) for d in ("SEND", "EDIT", "CANCEL")}
            print(f"{name:<8}{len(recs):>6}{passes:>6}{fails:>6}{len(rj):>8}  "
                  f"{cnt['SEND']}/{cnt['EDIT']}/{cnt['CANCEL']}")
        total = int(time.time() - self.t0)
        print(f"전체 걸린 시간: {total // 60}분 {total % 60}초")
        print(f"총 글 수: {len(self.posts)}")
        print(f"세션 폴더: {self.dir}")

    # ── 실행 ──
    def run(self):
        problem = input("문제를 입력하세요: ").strip()
        if not problem:
            print("문제가 비어 있어 종료합니다.")
            return
        self.dir = WORKDIR_ROOT / f"v2-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "chat.md").write_text("", encoding="utf-8")
        (self.dir / "summary.md").write_text("# 합의된 요약 (전원 동의한 것만)\n", encoding="utf-8")
        (self.dir / "draft.md").write_text("# 산출물\n", encoding="utf-8")
        (self.dir / "log.jsonl").write_text("", encoding="utf-8")
        print(f"세션 폴더: {self.dir}\n")
        self.t0 = time.time()

        try:
            with self.chat_lock:
                self._append(HUMAN, "전체", problem)
            self.first_opinions()

            self.say("\n=== 2단계: 토의 (순서 없음, 새 글이 생기면 깨움) ===")
            workers = [threading.Thread(target=self.worker, args=(n,), daemon=True) for n in NAMES]
            for t in workers:
                t.start()
            reason = self.main_loop()
            with self.cond:
                self.stop = True
                self.cond.notify_all()
            for t in workers:
                t.join()
            self.say(f"\n토의 종료: {reason}")
            self.conclude()
        except KeyboardInterrupt:
            self.stop = True
            print("\n(Ctrl+C로 중단)")
        self.print_stats()


if __name__ == "__main__":
    Engine().run()
