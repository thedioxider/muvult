from aiogram.fsm.state import State, StatesGroup


class UploadStates(StatesGroup):
    idle = State()
    awaiting_confirmation = State()
