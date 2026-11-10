import { expect, test, vi } from "vitest";
import { refresh } from "./api";

test("two refreshes at once send one request", async () => {
  // two tabs or two requests reusing one refresh token would look like theft and end the session
  const fetch = vi.fn(() => new Promise((ok) => setTimeout(() => ok(new Response("{}", { status: 401 })), 10)));
  vi.stubGlobal("fetch", fetch);
  await Promise.all([refresh(), refresh()]);
  expect(fetch).toHaveBeenCalledTimes(1);
  await refresh();
  expect(fetch).toHaveBeenCalledTimes(2); // a later refresh is a new request
});
