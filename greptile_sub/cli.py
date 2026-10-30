import argparse
import json
import logging
import re
import sys
import time
from dataclasses import asdict

from greptile_sub import github, learn, store
from greptile_sub.llm import LLMError
from greptile_sub.review import Review, comment_body, format_finding, review_pr

logger = logging.getLogger("greptile_sub")

PR_REF = re.compile(r"(?:https://github\.com/)?([\w.-]+/[\w.-]+)(?:#|/pull/)(\d+)")


def parse_pr(ref: str) -> tuple[str, int]:
    m = PR_REF.fullmatch(ref.strip().rstrip("/"))
    if not m:
        raise argparse.ArgumentTypeError(f"expected OWNER/REPO#N or a PR URL, got {ref!r}")
    return m.group(1), int(m.group(2))


def print_review(review: Review) -> None:
    print(f"\n=== {review.repo}#{review.number} @ {review.head_sha[:8]} ===\n")
    print(review.body())
    for f in review.inline:
        print(f"\n--- {f['path']}:{f['line']}\n{format_finding(f)}")
    for f in review.rejected:
        print(f"\n(dropped by verify) {f['path']}:{f['line']} {f['title']}: {f['verify_reason']}")
    for f in review.duplicates:
        print(f"\n(already raised) {f['path']}:{f['line']} {f['title']}: {f['duplicate_of']}")


def publish(db, review: Review, post: bool) -> None:
    url = None
    if post and not review.has_news():
        logger.info("Nothing new on %s#%d; not posting", review.repo, review.number)
    elif post:
        posted = github.post_review(
            review.repo, review.number, review.head_sha, review.body(), review.comments()
        )
        url = posted["html_url"]
        by_body = {comment_body(f): f for f in review.inline}
        by_line = {(f["path"], f["line"]): f for f in review.inline}
        for c in github.review_comments(review.repo, review.number, posted["id"]):
            line = c.get("line") or c.get("original_line")
            if finding := by_body.get(c["body"]) or by_line.get((c["path"], line)):
                store.record_comment(db, c["id"], review, finding)
        logger.info("Posted %s", url)
    store.record(db, review, url)


def cmd_review(args) -> int:
    repo, number = args.pr
    db = store.connect()
    review = review_pr(repo, number, verify=not args.no_verify, lessons=learn.lessons(db, repo))
    if args.json:
        print(json.dumps(asdict(review), indent=2))
    else:
        print_review(review)
    publish(db, review, args.post)
    return 0


def cmd_learn(args) -> int:
    db = store.connect()
    for repo in args.repos:
        decided = learn.collect(db, repo)
        print(f"{repo}: {decided} newly decided; {store.outcome_counts(db, repo)}")
    return 0


def cmd_lessons(args) -> int:
    db = store.connect()
    print(f"{args.repo}: {store.outcome_counts(db, args.repo)}")
    for lesson in learn.lessons(db, args.repo):
        print(f"- {lesson}")
    return 0


def cmd_watch(args) -> int:
    db = store.connect()
    logger.info("Watching %s every %ds (%s)", ", ".join(args.repos), args.interval,
                "posting" if args.post else "dry run")
    while True:
        for repo in args.repos:
            try:
                learn.collect(db, repo)
                prs = github.open_prs(repo)
            except (LLMError, github.GitHubError) as e:
                logger.error("%s: %s", repo, e)
                continue
            for pr in prs:
                seen = store.reviewed_shas(db, repo, pr["number"])
                if pr["isDraft"] or pr["headRefOid"] in seen or (seen and not args.on_push):
                    continue
                try:
                    review = review_pr(repo, pr["number"], lessons=learn.lessons(db, repo))
                    print_review(review)
                    publish(db, review, args.post)
                except (LLMError, github.GitHubError) as e:
                    logger.error("%s#%d: %s", repo, pr["number"], e)
        if args.once:
            return 0
        time.sleep(args.interval)


def main() -> None:
    parser = argparse.ArgumentParser(prog="greptile-sub")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("review", help="review one PR")
    p.add_argument("pr", type=parse_pr, help="OWNER/REPO#N or PR URL")
    p.add_argument("--post", action="store_true", help="post the review to GitHub (default: print only)")
    p.add_argument("--no-verify", action="store_true", help="skip the second verification pass")
    p.add_argument("--json", action="store_true", help="print the raw review as JSON")
    p.set_defaults(func=cmd_review)

    w = sub.add_parser("watch", help="poll repos and review new PRs")
    w.add_argument("repos", nargs="+", help="OWNER/REPO")
    w.add_argument("--post", action="store_true", help="post reviews to GitHub (default: print only)")
    w.add_argument("--interval", type=int, default=120, help="seconds between polls")
    w.add_argument("--on-push", action="store_true", help="also re-review when a PR gets new commits")
    w.add_argument("--once", action="store_true", help="poll once and exit")
    w.set_defaults(func=cmd_watch)

    l = sub.add_parser("learn", help="collect feedback on posted comments")
    l.add_argument("repos", nargs="+", help="OWNER/REPO")
    l.set_defaults(func=cmd_learn)

    s = sub.add_parser("lessons", help="show what the reviewer has learned for a repo")
    s.add_argument("repo", help="OWNER/REPO")
    s.set_defaults(func=cmd_lessons)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        sys.exit(args.func(args))
    except (LLMError, github.GitHubError) as e:
        logger.error("%s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
