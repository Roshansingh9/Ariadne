from shop.service import OrderService


def main():
    return OrderService().place([{"price": 3}])


if __name__ == "__main__":
    main()
