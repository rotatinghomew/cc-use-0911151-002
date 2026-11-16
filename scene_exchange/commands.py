from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from scene_exchange.coordinator import Application, run, verify


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog='scene_exchange', description='人工智能开放场景需求撮合与试点治理平台')
    commands = root.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run")
    execute.add_argument("--fixture", required=True)
    execute.add_argument("--database", required=True)
    execute.add_argument("--output", required=True)
    check = commands.add_parser("verify")
    check.add_argument("--database", required=True)
    check.add_argument("--output", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("--database", required=True)

    selection = commands.add_parser("record-selections", help="保存入选前的需求快照及未入选对照需求")
    selection.add_argument("--database", required=True)
    selection.add_argument("--round", required=True)
    selection.add_argument("--batch", required=True)
    selection.add_argument("--fixture", required=True, help="包含 selections 列表的 JSON 文件")
    selection.add_argument("--recorded-at")

    observation = commands.add_parser("record-observations", help="补报、更正或撤回观察结果")
    observation.add_argument("--database", required=True)
    observation.add_argument("--round", required=True)
    observation.add_argument("--batch", required=True)
    observation.add_argument("--period", required=True)
    observation.add_argument("--kind", required=True,
                             choices=["initial", "supplemental", "correction", "retraction"])
    observation.add_argument("--fixture", required=True, help="包含 observations 列表的 JSON 文件")
    observation.add_argument("--note", default="")
    observation.add_argument("--recorded-at")

    review = commands.add_parser("review-pilots", help="复盘试点效果并生成续期待审意见")
    review.add_argument("--database", required=True)
    review.add_argument("--round", required=True)
    review.add_argument("--as-of")
    review.add_argument("--refresh", action="store_true", help="同时刷新待审议的续期意见")

    pending = commands.add_parser("pending-opinions")
    pending.add_argument("--database", required=True)
    pending.add_argument("--round", required=True)

    decide = commands.add_parser("decide-renewal", help="对待审议意见作出续期决定并固定依据")
    decide.add_argument("--database", required=True)
    decide.add_argument("--round", required=True)
    decide.add_argument("--opinion", required=True)
    decide.add_argument("--outcome", required=True,
                        choices=["renew", "conditional_renew", "do_not_renew", "defer"])
    decide.add_argument("--approver", required=True)
    decide.add_argument("--note", default="")

    decisions = commands.add_parser("renewal-decisions")
    decisions.add_argument("--database", required=True)
    decisions.add_argument("--round", required=True)

    reproduce = commands.add_parser("reproduce-decision", help="核对续期决定依据的观察结果")
    reproduce.add_argument("--database", required=True)
    reproduce.add_argument("--decision", required=True)

    return root


def _read_fixture(path: str | Path, key: str, batch_id: str | None = None) -> list[Mapping[str, Any]]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read fixture: {exc}") from exc
    if isinstance(value, Mapping):
        for candidate in (key, "batches", "observation_batches", "selections"):
            if candidate in value:
                value = value[candidate]
                break
    if isinstance(value, list) and batch_id and all(
            isinstance(item, Mapping) and "observations" in item and "batch_id" in item for item in value):
        matches = [item for item in value if item["batch_id"] == batch_id]
        if len(matches) != 1:
            raise ValueError(f"fixture does not contain exactly one batch named {batch_id}")
        value = matches[0]["observations"]
    if not isinstance(value, list):
        raise ValueError(f"fixture must contain a {key} list")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "run":
        result = run(args.fixture, args.database, args.output)
    elif args.command == "verify":
        result = verify(args.database, args.output)
    elif args.command == "inspect":
        result = Application(args.database).inspect()
    else:
        application = Application(args.database)
        if args.command == "record-selections":
            result = application.record_selection_batch(
                args.round, args.batch, _read_fixture(args.fixture, "selections"),
                recorded_at=args.recorded_at)
        elif args.command == "record-observations":
            result = application.record_observation_batch(
                args.round, args.batch, args.period, args.kind,
                _read_fixture(args.fixture, "observations", batch_id=args.batch), note=args.note,
                recorded_at=args.recorded_at)
        elif args.command == "review-pilots":
            review = application.effect_review(args.round, as_of=args.as_of)
            if args.refresh:
                refreshed = application.refresh_renewal_opinion(args.round, as_of=args.as_of)
                result = refreshed | {"review": review}
            else:
                result = review
        elif args.command == "pending-opinions":
            result = {"round_id": args.round, "opinions": application.pending_opinions(args.round)}
        elif args.command == "decide-renewal":
            result = application.decide_renewal(args.round, args.opinion, args.outcome,
                                                args.approver, note=args.note)
        elif args.command == "renewal-decisions":
            result = {"round_id": args.round, "decisions": application.renewal_decisions(args.round)}
        else:
            result = application.reproduce_renewal_decision({"decision_id": args.decision})
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
