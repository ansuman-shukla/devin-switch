import sys

from devin_switch import cli, desktop


def main() -> int:
    if sys.argv[1:] == ["--desktop-bridge"]:
        return desktop.main()
    return cli.main()


if __name__ == "__main__":
    raise SystemExit(main())
