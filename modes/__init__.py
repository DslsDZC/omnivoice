"""模式包初始化"""
from modes.base import BaseMode, ModeResult
from modes.conference import ConferenceMode
from modes.serial import EnhancedSerialMode as SerialMode

# 争吵模式已集成到会议模式中，不再作为独立模式
# from modes.debate import DebateMode

__all__ = [
    'BaseMode', 'ModeResult',
    'ConferenceMode', 'SerialMode'
]