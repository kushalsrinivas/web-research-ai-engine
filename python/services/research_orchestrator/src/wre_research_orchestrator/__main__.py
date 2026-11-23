from wre_pipeline.service import run_service


def main() -> None:
    run_service("research-orchestrator", 8089)


if __name__ == "__main__":
    main()
