const { Client } = require("./api");

function boot() {
  const c = new Client();
  c.getOrder("1");
}

boot();
