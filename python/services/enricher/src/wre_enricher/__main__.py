from wre_pipeline.service import run_service


def main() -> None:
    run_service("enricher", 8086)


if __name__ == "__main__":
    main()
