"""开发环境初始化入口：创建幂等的演示业务数据。"""

from app.database import seed_demo_data


if __name__ == "__main__":
    # 初始化是幂等的：已有业务数据时不会重复插入演示记录。
    seed_demo_data()
    # 命令行入口只反馈完成状态，不把数据库内容打印到终端。
    print("Demo database initialized.")
