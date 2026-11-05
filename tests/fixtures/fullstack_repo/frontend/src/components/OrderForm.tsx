import { createOrder } from "../api/orders";

export function OrderForm() {
  const handleSubmit = async () => {
    await createOrder([]);
  };
  return (
    <form>
      <button onClick={handleSubmit}>Place order</button>
    </form>
  );
}
