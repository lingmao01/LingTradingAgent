import { fireEvent, render, screen } from '@testing-library/react';
import { AdvisorAnswer, isAdvisorReport } from '../AdvisorAnswer';
const report = { schema_version: 2, snapshot_sha256: 'snapshot', summary_text: '增仓：未触发。减仓：未触发。换仓：证据不足。', full_text: '完整证据与来源，保留未配置和未知条件。', signal_cards: [], action_cards: [] };
it('keeps the conclusion first and exposes evidence with an accessible disclosure', () => {
  render(<AdvisorAnswer report={report} />);
  expect(screen.getByText(/增仓：未触发/)).toBeVisible();
  expect(screen.queryByText(/完整证据与来源/)).not.toBeInTheDocument();
  const toggle = screen.getByRole('button', { name: '展开完整报告' });
  expect(toggle).toHaveAttribute('aria-expanded', 'false');
  fireEvent.click(toggle);
  expect(toggle).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByText(/完整证据与来源/)).toBeVisible();
  expect(screen.getByRole('button', { name: '复制结论' })).toBeVisible();
  expect(screen.getByRole('button', { name: '导出完整报告' })).toBeVisible();
});
it('rejects unknown schemas and malformed projections for Markdown fallback', () => {
  expect(isAdvisorReport({ ...report, schema_version: 3 })).toBe(false);
  expect(isAdvisorReport({ ...report, full_text: null })).toBe(false);
  expect(isAdvisorReport(report)).toBe(true);
});

it('copies only the concise conclusion while exporting the complete source-backed report', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  const create = vi.fn().mockReturnValue('blob:controlled-advisor-report');
  const revoke = vi.fn();
  Object.defineProperty(URL, 'createObjectURL', { value: create, configurable: true });
  Object.defineProperty(URL, 'revokeObjectURL', { value: revoke, configurable: true });
  const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  render(<AdvisorAnswer report={report} />);
  fireEvent.click(screen.getByRole('button', { name: '复制结论' }));
  expect(writeText).toHaveBeenCalledWith(report.summary_text);
  expect(writeText).not.toHaveBeenCalledWith(report.full_text);
  fireEvent.click(screen.getByRole('button', { name: '导出完整报告' }));
  expect(create).toHaveBeenCalledOnce();
  const exported = create.mock.calls[0][0] as Blob;
  expect(exported.type).toBe('text/markdown;charset=utf-8');
  const text = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = reject;
    reader.readAsText(exported);
  });
  expect(text).toBe(report.full_text);
  expect(click).toHaveBeenCalledOnce();
  expect(click.mock.instances[0].download).toBe('K-ETF-snapshot.md');
});

it('keeps unheld opportunities and invalidation visible in concise research text after remount', () => {
  const researchReport = { ...report,
    summary_text: '增仓：未触发。减仓：未触发。换仓：先核对准入。持仓外观察：黄金。改判条件：兑现证据或共同风险改变。',
    full_text: '完整资料含反证、准入未知及来源。',
  };
  const first = render(<AdvisorAnswer report={researchReport} />);
  expect(screen.getByText(/持仓外观察：黄金/)).toBeVisible();
  expect(screen.getByText(/改判条件：兑现证据/)).toBeVisible();
  first.unmount();
  render(<AdvisorAnswer report={JSON.parse(JSON.stringify(researchReport))} />);
  expect(screen.getByText(/持仓外观察：黄金/)).toBeVisible();
  expect(screen.getByRole('button', { name: '展开完整报告' })).toHaveAttribute('aria-expanded', 'false');
});
