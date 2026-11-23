from wre_pipeline.service import run_service


def main() -> None:
    run_service("llm-gateway", 8088)


if __name__ == "__main__":
    main()
