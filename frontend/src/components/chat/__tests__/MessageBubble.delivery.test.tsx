import { render, screen } from "@testing-library/react";
import { MessageBubble } from "../MessageBubble";
import type { StoredAgentMessage } from "@/stores/agent";

const message = (completion: string): StoredAgentMessage => ({
  id: "delivery", type: "answer", content: "已核对部分持仓，其余缺少净值。", timestamp: 1,
  meta: { delivery: { completion_status: completion } },
});

it("makes incomplete delivery visible while retaining the useful answer", () => {
  render(<MessageBubble msg={message("incomplete")} />);
  expect(screen.getByRole("status")).toHaveTextContent(/incomplete|未完成/i);
  expect(screen.getByText("已核对部分持仓，其余缺少净值。")).toBeVisible();
});

it("distinguishes partial delivery from completed analysis", () => {
  const view = render(<MessageBubble msg={message("partial")} />);
  expect(screen.getByRole("status")).toHaveTextContent(/partial|部分/i);
  view.rerender(<MessageBubble msg={message("complete")} />);
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});
