import { Button, Drawer } from './ui';
import type { ExitDialogChoice } from '../../shared/exit-dialog';

/** The parent keeps the request ID and sends the selected choice through the trusted bridge. */
export function ExitConfirmation({ onChoose, saveFailed = false }: { onChoose: (choice: ExitDialogChoice) => void; saveFailed?: boolean }) {
  return <Drawer title="还有内容未保存" side="center" className="exit-confirmation" onClose={() => onChoose('cancel')}>
    <style>{`.drawer-center .drawer-panel.exit-confirmation { min-height: 154px; }`}</style>
    {saveFailed && <p role="alert" style={{ margin: '16px 25px 0' }}>保存未完成，请重试。</p>}
    <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 10, padding: '31px 25px 24px' }}>
      <Button onClick={() => onChoose('cancel')}>继续编辑</Button>
      <Button variant="primary" onClick={() => onChoose('save')}>保存并退出</Button>
    </div>
  </Drawer>;
}
