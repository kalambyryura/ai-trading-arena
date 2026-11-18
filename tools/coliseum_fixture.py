"""Fictional read models for UI QA only. No market/API/order transport or author attribution."""
from copy import deepcopy
from datetime import datetime, timezone
import math
import time

from trading_lab.arena import ArenaError


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class ColiseumFixture:
    """Wrap a disposable offline lab with a larger, strictly display-only roster."""
    def __init__(self, lab, count=12):
        if count not in {2, 6, 12, 24}:
            raise ValueError("UI fixture size must be 2, 6, 12 or 24")
        self.lab, self.count = lab, count
        self.started = time.time()
        self.identity = ("c" * 30) + str(count).zfill(2)

    def __getattr__(self, name):
        return getattr(self.lab, name)

    def run(self, ended=False):
        now = time.time()
        cycle = int((now-self.started)/5)
        # Fixed slots, mixed states. This fixture is not routed through the executor.
        names = ["За трендом", "Контроль імпульсу", "Після відкату", "Повернення до середнього",
                 "Обсяг і свічка", "Короткий імпульс", "Терпіння", "Свічний аналіз",
                 "Тихий ринок", "Проти натовпу", "Свій план", "Новий погляд"]
        bots, strategies, phases, events = [], [], [], []
        inside_count = 2 if self.count == 2 else self.count // 2
        for i in range(self.count):
            identity = "ui-" + str(i+1).zfill(2)
            strategy_id = "fictional-" + str(i+1)
            is_unknown = self.count >= 6 and i == self.count-1
            retired = self.count >= 6 and i == self.count-2
            open_position = i < inside_count and not ended
            closing = open_position and i == inside_count-1
            position = None if is_unknown and not ended else (.0001 if i % 2 == 0 else -.0001) if open_position else 0
            result = round([3.24, -1.46, .81, -.73, 2.10, -.52][i % 6] + (math.sin(cycle/8+i)*.015 if open_position else 0), 8) if (open_position or retired or ended) else 0
            author = {"kind": "human" if i % 3 == 0 else "model", "name": "Вигаданий автор " + str(i+1),
                      "model": "UI fixture" if i % 3 else ""}
            strategy = {"id":strategy_id,"name":names[i % len(names)] + (" " + str(i//12+1) if i>=12 else ""),
                        "version":1,"author":author,"description":"Вигаданий учасник для перевірки інтерфейсу; торгової стратегії немає.","rules":{}}
            strategies.append(strategy)
            bot = {"id":identity,"strategy_id":strategy_id,"name":strategy["name"],"author":author,"avatar_kind":author["kind"],
                   "position_btc":position,"equity_usdt":None if is_unknown and not ended else 100+result,
                   "pnl_usdt":None if is_unknown and not ended else result,"fees_usdt":.0075,"funding_usdt":0,
                   "status":"DONE" if ended or retired else "CLOSING" if closing else "LONG" if open_position and position>0 else "SHORT" if open_position else "WAITING",
                   "stop_requested":retired or closing,"ai_cost_usd":.0000125,"ai_cost_complete":True,"ai_requests":1,
                   "curve":[{"at":stamp(now-300),"equity_usdt":100},{"at":stamp(now),"equity_usdt":100+result}],
                   "last_reason":"Позиція не підтверджена" if is_unknown else "Очікуємо формальних умов входу" if not open_position else "Ризик і позицію перевірено"}
            bots.append(bot)
            code = "finished" if ended or retired else "paused" if is_unknown else "closing" if closing else "jev" if i==1 else "position_checks" if open_position else "entry_checks"
            step = 6 if ended or retired or closing else 5 if code=="jev" and open_position else 4 if open_position else 1
            phases.append({"bot_id":identity,"current_phase":{"code":code,"step":step,"label":"Jev · оцінює позицію" if code=="jev" else "Перевіряє позицію" if open_position else "Очікує умов входу"},
                           "wait_reason":{"code":"fixture","message":bot["last_reason"]},
                           "costs":{"signed_fees_usdt":-.0075,"signed_funding_usdt":0,"complete":ended,
                                    "jev":{"known_cost_usd":.0000125,"complete":True}}})
            events.append({"seq":i+1,"at":stamp(self.started-60+i),"bot_id":identity,"step":step,"code":"fixture_state",
                           "message":strategy["name"]+": "+bot["last_reason"],"data":{"fixture_only":True,"position_btc":position}})
        bots.append({"id":"cash","strategy_id":"cash","name":"Скриня","avatar_kind":"baseline","position_btc":0,
                     "equity_usdt":100,"pnl_usdt":0,"fees_usdt":0,"funding_usdt":0,"ai_cost_usd":0,"status":"CASH"})
        events += [{"seq":self.count+cycle*2+1,"at":stamp(now-5),"bot_id":bots[0]["id"],"step":4,"code":"position_checks",
                    "message":"За трендом: позицію й ризикові межі перевірено.","data":{"fixture_only":True}},
                   {"seq":self.count+cycle*2+2,"at":stamp(now),"bot_id":bots[1]["id"],"step":5,"code":"jev_decision",
                    "message":"Контроль імпульсу: Jev оцінює утримання позиції.","data":{"fixture_only":True,"choice":"hold","cost_usd":.0000125}}]
        return {"id":self.identity if not ended else "d"*32,"context":"arena_live","mode":"OKX_LIVE","status":"DONE" if ended else "RUNNING",
                "fixture_only":True,"budget_usdt":100,"duration_seconds":1800,"created_at":stamp(self.started-300),"end_at":stamp(self.started+1500),
                "bots":bots,"strategies":strategies,"events":events,"frames":[],"history_complete":True,
                "latest_market":{"at":stamp(now),"mid":84000,"source":"SYNTHETIC-UI-FIXTURE"},"lifecycle":{"bots":phases},
                "manifest":{"participants":[{"bot_id":b["id"],"strategy_id":b["strategy_id"],"account_name":"Fixture " + str(i+1)} for i,b in enumerate(bots[:-1])]}}

    def status(self):
        result = self.lab.status()
        run, ended = self.run(), self.run(True)
        result["active"]["arena_live"] = run
        result["history"] = [{k:r[k] for k in ("id","context","mode","status","created_at","budget_usdt","duration_seconds")} | {"participants":self.count} for r in (run, ended)]
        return result

    def history(self, identity):
        if identity in {self.identity, "d"*32}:
            return deepcopy(self.run(identity == "d"*32))
        return self.lab.history(identity)

    def start(self, *args, **kwargs):
        raise ArenaError("Це display-only UI fixture. Скористайся звичайним offline preview для тесту запуску.")

    stop = start
    resume = start
