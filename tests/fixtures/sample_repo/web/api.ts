import axios from "axios";
import { BaseClient, Fetcher } from "./base";
import { formatPrice } from "./format";

/** HTTP client for the order API. */
export class Client extends BaseClient implements Fetcher {
  getOrder(id: string) {
    return this.request(id);
  }

  request(id: string) {
    return axios.get(`/orders/${id}`);
  }
}

export const showPrice = (p: number) => formatPrice(p);
