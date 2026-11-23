from wre_pipeline.service import run_service


def main() -> None:
    run_service("graph-builder", 8087)


if __name__ == "__main__":
    main()
