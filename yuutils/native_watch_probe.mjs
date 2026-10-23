// Isolated native test fixture: verifies actual dispatch, handles input before a model.
import fs from 'node:fs';
import bridge, { NativeUISafety } from './bridge.js';
export default function probe(pi) {
  fs.writeFileSync(process.env.YW_RECEIPT, JSON.stringify({ phase: 'loaded', sends: 0 }), { mode: 0o600 });
  const handlers = new Map();
  // Synthetic text deliberately includes literal newlines and a malformed JSON code.
  const rateError = 'Error: Retry failed after 3 attempts: OpenAI API error (429): {"message":"Rate limit exceeded\n [trace_id=synthetic\n code=20011]","type":"rate_limit_exceeded","code":"rate_limit_exceeded"}';
  const quotaError = 'Error: OpenAI API error (402): {"message":"API Key quota has been exhausted. | Used 2001 of\n 2000 in the current cycle.","type":"insufficient_quota","code":api_key_quota_exhausted"}';
  const overloadError = 'Our servers are currently overloaded. Please try again later.';
  let ctx, tui, editor, sends = 0, dispatched = 0, finishModal;
  pi.on('input', (event) => {
    if (event.source !== 'extension') return { action:
      ['/yw-reload', '/yw-exit'].includes(event.text) ? 'continue' : 'handled' };
    if (['/yw-reload', '/yw-exit'].includes(event.text)) return { action: 'continue' };
    if (event.text !== 'continue') throw Error('unexpected input');
    dispatched++;
    receipt('dispatched');
    return { action: 'handled' }; // Stop before auth checks or any model invocation.
  });
  const receipt = (phase) => fs.writeFileSync(process.env.YW_RECEIPT,
    JSON.stringify({ phase, sends, dispatched, editor: editor?.constructor.name,
      focus: tui?.getFocusedComponent() === editor,
      overlay: tui?.hasOverlayEntries, textEmpty: ctx?.ui.getEditorText() === '',
      paste: editor?.isInPaste }), { mode: 0o600 });
  bridge({ on(name, fn) { handlers.set(name, fn); },
    sendUserMessage(text, options) {
      if (text !== 'continue' || options.deliverAs !== 'followUp') throw Error('unexpected send');
      sends++; receipt('sent');
      pi.sendUserMessage(text, options);
    } });
  const editorState = () => JSON.stringify({ text: editor.getText(), expanded: editor.getExpandedText(),
    pastes: [...editor.pastes], counter: editor.pasteCounter, state: editor.state,
    buffer: editor.pasteBuffer, inPaste: editor.isInPaste, undo: editor.undoStack,
    history: editor.history, historyIndex: editor.historyIndex, lastAction: editor.lastAction });
  const preservation = async () => {
    for (const kind of ['text', 'compressed', 'image', 'in-flight']) {
      ctx.ui.setEditorText('');
      if (kind === 'text') editor.handleInput('ordinary draft');
      if (kind === 'compressed') {
        editor.handleInput('\x1b[200~' + 'long synthetic paste\n'.repeat(300) + '\x1b[201~');
        if (editor.pastes.size !== 1 || !editor.getExpandedText().includes('long synthetic paste\n'.repeat(300)))
          throw Error('compressed paste fixture failed');
      }
      if (kind === 'image') {
        editor.onPasteImagePath(process.env.YW_IMAGE);
        await new Promise(r => setTimeout(r, 100));
        if (!/\[Image #\d+\]/.test(editor.getText())) throw Error('image fixture failed');
      }
      if (kind === 'in-flight') editor.handleInput('\x1b[200~partial paste');
      const before = editorState(), shortcut = editor.onExtensionShortcut;
      const safety = new NativeUISafety(ctx);
      await safety.install();
      if (!safety.installed || editorState() !== before || editor.onExtensionShortcut !== shortcut)
        throw Error('install state preservation failed');
      // The exact pinned-native reset path calls setText with currentText.
      ctx.ui.setEditorComponent(undefined);
      if (editorState() !== before) throw Error('native reset state preservation failed');
      safety.close();
      if (editorState() !== before || editor.onExtensionShortcut !== shortcut)
        throw Error('close state preservation failed');
      if (editor.isInPaste) editor.handleInput('\x1b[201~');
    }
  };
  pi.registerCommand('yw-reload', { description: 'isolated no-model lifecycle test',
    handler: async (_args, commandCtx) => {
      ctx.ui.setEditorText('');
      editor.handleInput('\x1b[200~' + 'reload synthetic paste\n'.repeat(300) + '\x1b[201~');
      editor.onPasteImagePath(process.env.YW_IMAGE);
      await new Promise(r => setTimeout(r, 100));
      fs.writeFileSync(process.env.YW_RELOAD_STATE, JSON.stringify({ expanded: editor.getExpandedText() }), { mode: 0o600 });
      await commandCtx.reload();
    } });
  pi.registerCommand('yw-exit', { description: 'isolated graceful shutdown',
    handler: async (_args, commandCtx) => commandCtx.shutdown() });
  pi.on('session_start', async (event, context) => {
    ctx = context;
    receipt('entered');
    await ctx.ui.custom((t, _theme, _keys, done) => {
      tui = t; editor = t.getFocusedComponent();
      done();
      return { render: () => [], invalidate() {} };
    }, { overlay: true });
    receipt('captured');
    if (fs.existsSync(process.env.YW_RELOAD_STATE)) {
      // Native reload emits session_start while the loading box has focus.
      setTimeout(async () => {
        try {
          editor = tui.getFocusedComponent();
          await handlers.get('session_start')(event, ctx);
          const expected = JSON.parse(fs.readFileSync(process.env.YW_RELOAD_STATE, 'utf8'));
          if (editor.getExpandedText() !== expected.expanded || editor.pastes.size !== 1 ||
              !/\[Image #\d+\]/.test(editor.getText())) {
            fs.writeFileSync(process.env.YW_RECEIPT, JSON.stringify({ phase: 'reload-preservation-failed',
              expandedEqual: editor.getExpandedText() === expected.expanded, pastes: editor.pastes.size,
              image: /\[Image #\d+\]/.test(editor.getText()) }), { mode: 0o600 });
            return;
          }
          receipt('reloaded');
        } catch (error) {
          fs.writeFileSync(process.env.YW_RECEIPT, JSON.stringify({ phase: 'reload-check-error',
            name: error.name, category: 'fixture' }), { mode: 0o600 });
        }
      }, 500);
      return;
    }
    await preservation();
    ctx.ui.setEditorText('preserved setup draft');
    const before = editor;
    const shortcut = editor.onExtensionShortcut;
    await handlers.get('session_start')(event, ctx);
    if (editor !== before || ctx.ui.getEditorText() !== 'preserved setup draft' ||
        editor.onExtensionShortcut !== shortcut) throw Error('editor preservation failed');
    ctx.ui.setEditorText('');
    receipt('ready');
    ctx.ui.onTerminalInput(() => {
      if (lastCase === 'input') receipt('input-observed');
    });
  });
  let lastCase = fs.existsSync(process.env.YW_RELOAD_STATE) ?
    fs.readFileSync(process.env.YW_CASE, 'utf8') : '';
  const timer = setInterval(async () => {
    if (!ctx) return;
    try {
      const arg = fs.readFileSync(process.env.YW_CASE, 'utf8');
      if (arg === lastCase) return;
      lastCase = arg;
      await runCase(arg);
    } catch (error) {
      if (lastCase) fs.writeFileSync(process.env.YW_RECEIPT,
        JSON.stringify({ phase: 'probe-error', name: error.name,
          category: String(error.message).includes('matches') ? 'keybindings-matches' :
            String(error.message).includes('paste') ? 'paste' :
            String(error.message).includes('submit') ? 'submit' : 'other' }), { mode: 0o600 });
    }
  }, 50);
  pi.on('session_shutdown', async () => { clearInterval(timer); await handlers.get('session_shutdown')(); });
  const runCase = async (arg) => {
    if (finishModal) { finishModal(); finishModal = undefined; await new Promise(r => setTimeout(r, 20)); }
    if (arg === 'prepare-reload') {
      ctx.ui.setEditorComponent(undefined);
      editor = tui.getFocusedComponent();
      await handlers.get('session_start')({}, ctx);
      ctx.ui.setEditorText('');
      receipt(arg);
      return;
    }
    if (arg === 'invoke-reload') { pi.sendUserMessage('/yw-reload', { expandPromptTemplates: true }); return; }
    if (arg === 'invoke-exit') { pi.sendUserMessage('/yw-exit', { expandPromptTemplates: true }); return; }
    if (arg === 'prepare-exit') { ctx.ui.setEditorText(''); receipt(arg); return; }
    if (arg === 'rate-settled') {
      handlers.get('agent_settled')({}, ctx);
      receipt(arg);
      return;
    }
    if (arg === 'text') ctx.ui.setEditorText('synthetic draft');
    else if (arg === 'image') {
      ctx.ui.setEditorText('');
      editor.onPasteImagePath(process.env.YW_IMAGE);
      await new Promise(r => setTimeout(r, 100));
      if (!/\[Image #\d+\]/.test(ctx.ui.getEditorText())) throw Error('image insertion failed');
    } else if (arg === 'paste') {
      ctx.ui.setEditorText('');
      editor.handleInput('\x1b[200~');
    }
    else if (arg === 'modal' || arg === 'overlay') {
      ctx.ui.setEditorText('');
      void ctx.ui.custom((_t, _theme, _keys, done) => {
        finishModal = done;
        return { render: () => ['isolated modal'], handleInput(data) {
          if (data === '\x1b') done();
        }, invalidate() {} };
      }, { overlay: arg === 'overlay' });
      await new Promise(r => setTimeout(r, 30));
    } else if (arg === 'unknown-editor') {
      const hooks = Object.fromEntries(['onEscape', 'onCtrlD', 'onPasteImage', 'onEmptyPaste',
        'onPasteImagePath', 'canDequeue', 'onExtensionShortcut'].map(k => [k, editor[k]]));
      ctx.ui.setEditorComponent(() => editor);
      Object.assign(editor, hooks);
    } else {
      if (editor.isInPaste) editor.handleInput('\x1b[201~');
      ctx.ui.setEditorText('');
    }
    handlers.get('agent_start')({}, ctx);
    const errorMessage = arg.startsWith('rate') ? rateError :
      arg === 'quota-mixed' ? quotaError + '\nrate_limit_exceeded\n' + overloadError :
      arg === 'quota' ? quotaError : overloadError;
    const failed = { role: 'assistant', stopReason: 'error', errorMessage };
    const messages = arg === 'success' ? [{ ...failed, stopReason: 'stop' }] :
      arg === 'aborted' ? [{ ...failed, stopReason: 'aborted' }] :
      arg === 'old-error' ? [failed, { role: 'assistant', stopReason: 'stop' }] : [failed];
    handlers.get('agent_end')({ messages }, ctx);
    if (arg !== 'rate-unsettled') handlers.get('agent_settled')({}, ctx);
    receipt(arg);
  };
}
