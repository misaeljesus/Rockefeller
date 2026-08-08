"""
Punto de entrada de Rockefeller.

  1) cp .env.example .env  y añade tus claves API de Binance
  2) Elige el modo en config.py: paper → testnet → live (en ese orden)
  3) python main.py
"""
import logging
import os
import sys

from dotenv import load_dotenv

from config import SETTINGS
from rockefeller.bot import Rockefeller


def setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, SETTINGS.run.log_level),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler("rockefeller.log")],
    )


def main() -> None:
    load_dotenv()
    setup_logging()
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    if SETTINGS.run.mode != "paper" and (not api_key or not api_secret):
        raise SystemExit("Faltan BINANCE_API_KEY / BINANCE_API_SECRET en .env")
    if SETTINGS.run.mode == "live":
        logging.warning("MODO LIVE: dinero real. Verifica haber validado la "
                        "estrategia en paper y testnet antes de continuar.")
    Rockefeller(api_key, api_secret, SETTINGS).run()


if __name__ == "__main__":
    main()
