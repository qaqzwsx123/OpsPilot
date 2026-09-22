"""开发环境初始化入口：创建幂等的演示业务数据。"""

from app.database import seed_demo_data


if __name__ == "__main__":
    seed_demo_data()
    print("Demo database initialized.")
