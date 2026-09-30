"""全局配置：阈值、费率、目录与演示常量。"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "museum.db"
PHOTO_DIR = DATA_DIR / "photos"
LANG_DIR = DATA_DIR / "langpacks"

# 电量阈值：借出与重新入柜共用
READY_BATTERY = 80
# 每次归还借用时长（演示用 4 小时）
LOAN_HOURS = 4
# 费率（演示数据，单位：元）
DEPOSIT_DEFAULT = 200
OVERDUE_FEE = 20
EARPHONE_FEE = 50
STRAP_FEE = 20
DAMAGE_FEE = 150

# 后台任务节拍（秒）
OVERDUE_TICK = 5
CHARGE_TICK = 2
CHARGE_STEP = 14  # 每拍冲入电量

HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8765

ROLE_NAMES = {
    "clerk": "讲解员/柜台",
    "technician": "消毒充电员",
    "repair": "维修员",
    "supervisor": "值班主管",
}

DEVICE_STATUS = {
    "available": "在柜可借",
    "on_loan": "在借",
    "return_check": "归还检查",
    "pending_disinfection": "待消毒",
    "pending_charge": "待充电",
    "charging": "充电中",
    "repair": "维修中",
}
