export class BaseClient {
  close(): void {}
}

export interface Fetcher {
  request(id: string): Promise<unknown>;
}
