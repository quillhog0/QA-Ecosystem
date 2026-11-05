import argparse


def main(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="acme-tool")
    parser.add_argument("--format", default="json")
    return parser.parse_args(argv)

