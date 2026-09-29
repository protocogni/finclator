"""Shared fixture: an in-memory SQLite finclator DB with three accounts, four tweets, two calls, one outcome."""
from pathlib import Path

import pytest

from src import db


@pytest.fixture
def conn():
    c = db.connect(Path(":memory:"), url="")
    c.executescript("""
    INSERT INTO accounts(handle, school, active, sampling, rate_per_year) VALUES
      ('alice','Crypto',1,NULL,300), ('bob','Macro',1,'keyword',5000), ('carol','Quant',1,NULL,10);
    INSERT INTO tweets(tweet_id, handle, created_at, text, source, assets_hint, relevant) VALUES
      ('1','alice','2024-01-05T10:00:00+00:00','BTC to 100k this year','twitterapi','BTC',1),
      ('2','alice','2024-02-05T10:00:00+00:00','gold looks tired','twitterapi','GOLD',1),
      ('3','bob','2023-06-01T10:00:00+00:00','SPX bubble','twitterapi','SPX',1),
      ('4','carol','2026-09-01T10:00:00+00:00','hello','twitterapi','',0);
    INSERT INTO classified_by(tweet_id, model, at) VALUES
      ('1','m','2026-09-01 00:00:00'),('2','m','2026-09-01 00:00:00'),('3','m','2026-09-01 00:00:00');
    INSERT INTO calls(id, tweet_id, handle, asset, direction, horizon, confidence, price_target, quote, called_at, model, gate_p) VALUES
      (1,'1','alice','BTC','BUY','MEDIUM',0.9,100000,'BTC to 100k','2024-01-05T10:00:00+00:00','m',0.95),
      (2,'3','bob','SPX','SELL','LONG',0.6,NULL,'SPX bubble','2023-06-01T10:00:00+00:00','m',NULL);
    INSERT INTO outcomes(call_id, entry_date, exit_date, entry_close, exit_close, return_pct, threshold_pct, actual, result,
                         target_hit, extreme, evaluated_at)
      VALUES (1,'2024-01-05','2025-01-04',44000,98000,122.7,25.0,'BUY','CORRECT',0,98000,'2025-01-05 00:00:00');
    INSERT INTO trust(model, handle, asset, horizon, n, correct, score, computed_at) VALUES
      ('m','alice','BTC','MEDIUM',1,1.0,0.545,'x'), ('m','alice','*','*',1,1.0,0.545,'x');
    INSERT INTO prices(asset, date, close) VALUES ('BTC','2026-09-22',85000),('GOLD','2026-09-22',4350),('SPX','2026-09-22',7780);
    INSERT INTO gate(tweet_id, model, p_call, stances, at) VALUES ('1','jev-1.13.0',0.95,'{"BTC":"up"}','2026-09-23 00:00:00');
    """)
    c.commit()
    return c


@pytest.fixture(autouse=True)
def _model(monkeypatch):
    monkeypatch.setenv("FINCLATOR_ACTIVE_MODEL", "m")
