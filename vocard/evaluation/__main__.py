import argparse
import json
from pathlib import Path

from vocard.config import Settings
from vocard.db import Repository
from vocard.evaluation.dataset import DEFAULT_DATASET, digest, load_dataset, sha256, validate_splits
from vocard.evaluation.grading import export_review, import_human, model_grade, report
from vocard.evaluation.runner import load_run, run, write_json
from vocard.evaluation.runtime import ProductionCorpus, ReferenceCorpus, RunConfig
from vocard.storage import PrivateS3


def load_pages(path, manifest):
    raw = json.loads(Path(path).read_text())
    if sha256(path) != manifest["reference_ocr_sha256"]:
        raise ValueError("Frozen reference OCR content changed")
    if raw["source"]["sha256"] != manifest["source"]["sha256"]:
        raise ValueError("Reference PDF hash changed")
    return {p["pdf_page_number"]: p for p in raw["pages"]}


def main():
    parser = argparse.ArgumentParser(description="Fixed Vocard Q&A evaluation")
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    execute = commands.add_parser("run")
    execute.add_argument("--split", choices=("tuning", "heldout"), default="tuning")
    execute.add_argument("--config", type=Path, required=True)
    execute.add_argument("--mode", choices=("reference", "production"), default="reference")
    execute.add_argument(
        "--ocr", type=Path, default=Path("output/pdf/textbook-extraction/textbook.json")
    )
    execute.add_argument("--curriculum", type=Path, default=Path("../Vocard/data"))
    execute.add_argument("--book-id", default="devils-100")
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--selection", type=Path)
    freeze = commands.add_parser("freeze")
    freeze.add_argument("--tuning-run", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    for name in ("review-packet", "grade-model"):
        command = commands.add_parser(name)
        command.add_argument("--run", type=Path, required=True)
        command.add_argument(
            "--ocr", type=Path, default=Path("output/pdf/textbook-extraction/textbook.json")
        )
        command.add_argument("--output", type=Path, required=True)
        if name == "grade-model":
            command.add_argument("--config", type=Path, required=True)
            command.add_argument("--grader", required=True)
    summarize = commands.add_parser("report")
    summarize.add_argument("--run", type=Path, required=True)
    summarize.add_argument("--grades", nargs="*", type=Path, default=[])
    summarize.add_argument("--output", type=Path, required=True)
    human = commands.add_parser("import-human")
    human.add_argument("--run", type=Path, required=True)
    human.add_argument("--packet", type=Path, required=True)
    human.add_argument("--reviewer", required=True)
    human.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            print(json.dumps(validate_splits(args.dataset), ensure_ascii=False, indent=2))
        elif args.command == "freeze":
            manifest, _ = load_run(args.tuning_run)
            if manifest["split"] != "tuning":
                raise ValueError("Freeze only a completed tuning run")
            write_json(
                args.output,
                {
                    **manifest["protocol"],
                    "resolved_config_sha256": digest(manifest["config"]),
                    "tuning_run_id": manifest["run_id"],
                    "tuning_run_path": str(args.tuning_run.resolve()),
                    "tuning_predictions_sha256": sha256(args.tuning_run / "predictions.jsonl"),
                },
            )
        elif args.command == "run":
            config = RunConfig.model_validate_json(args.config.read_text())
            config.validate_comparison()
            manifest, _ = load_dataset(args.dataset, args.split)
            if args.mode == "production":
                settings = Settings.from_env()
                corpus = ProductionCorpus(
                    settings,
                    Repository(settings.db_path),
                    PrivateS3(settings.bucket),
                    args.book_id,
                    manifest,
                    config,
                )
            else:
                settings = Settings(
                    db_path=Path("unused-evaluation.sqlite3"),
                    bucket="offline-evaluation",
                    jwt_secret="unused-" * 8,
                )
                corpus = ReferenceCorpus(args.ocr, args.curriculum, manifest, config, args.book_id)
            result = run(
                args.dataset,
                args.split,
                config,
                args.config,
                corpus,
                settings,
                args.output,
                json.loads(args.selection.read_text()) if args.selection else None,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "report":
            report(args.run, args.dataset, args.grades, args.output)
        elif args.command == "import-human":
            import_human(args.run, args.dataset, args.packet, args.reviewer, args.output)
        else:
            manifest, _ = load_run(args.run)
            labels, _ = load_dataset(args.dataset, manifest["split"])
            if (
                sha256(args.dataset / "manifest.json")
                != manifest["protocol"]["dataset_manifest_sha256"]
            ):
                raise ValueError("Dataset no longer matches run")
            pages = load_pages(args.ocr, labels)
            if args.command == "review-packet":
                export_review(args.run, args.dataset, pages, args.output)
            else:
                config = RunConfig.model_validate_json(args.config.read_text())
                config.validate_comparison()
                grader = next(c for c in config.candidates if c.candidate_id == args.grader)
                if grader.provider not in ("openai", "ollama"):
                    raise ValueError("Semantic model grading requires an actual model")
                model_grade(args.run, args.dataset, pages, grader, config, args.output)
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        parser.exit(2, f"Evaluation failed: {type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
