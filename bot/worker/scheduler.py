from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from bot.worker.broker import broker, schedule_source

scheduler = TaskiqScheduler(broker, sources=[LabelScheduleSource(broker), schedule_source])
