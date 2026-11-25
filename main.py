"""Unternehmensrecherche nach primären und sekundären Quellen."""
import argparse
import os
from pathlib import Path

from research_app.cli import ArgumentParser


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['primary', 'secondary', 'research', 'kununu', 'kununu-browser', 'unternehmensregister', 'gewerkschaften', 'karriere', 'implisense', 'documents', 'output-contract'],
                        help='primary: Unternehmensregister, Kununu, Gewerkschaften, Karriereseiten; secondary: gespeicherte Implisense-Nachrichten')
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    remaining = args.arguments
    if args.command == 'output-contract':
        from research_app.output_contract import main as contract_main
        return contract_main(remaining)
    if args.command == 'kununu-browser':
        from research_app.kununu_browser import main as browser_main
        return browser_main(remaining)
    if args.command == 'documents':
        from research_app.register_documents import main as documents_main
        return documents_main(remaining)
    if args.command == 'implisense':
        from research_app.implisense_browser import main as implisense_main
        try:
            return implisense_main(remaining)
        except (Exception, KeyboardInterrupt) as error:
            print(f'Implisense-Browser: {error}')
            return 1
    if args.command == 'unternehmensregister':
        from research_app.register_jobs import main as register_main
        return register_main(remaining)
    from research_app.research import main as research_main
    return research_main(remaining + (['--source', args.command] if args.command in (
        'primary', 'secondary', 'kununu', 'gewerkschaften', 'karriere') else []))


if __name__ == '__main__':
    os.chdir(Path(__file__).resolve().parent)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Abgebrochen.')
        raise SystemExit(130)
    except Exception as error:
        print(f'Fehler: {error}')
        raise SystemExit(1)
