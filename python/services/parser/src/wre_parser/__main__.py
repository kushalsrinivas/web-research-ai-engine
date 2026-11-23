from wre_pipeline.service import run_service


def main() -> None:
    run_service("parser", 8085)


if __name__ == "__main__":
    main()
