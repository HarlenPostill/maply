import { useEffect, useState } from 'react';
import { api } from './api';

type Settings = {
  provider: 'antigravity' | 'openrouter';
  antigravity_model: string;
  openrouter_model: string;
  api_key_configured: boolean;
};

export default function ModelSettings() {
  const [open, setOpen] = useState(false);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [key, setKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [models, setModels] = useState<{ id: string; name: string }[]>([]);
  const [catalogError, setCatalogError] = useState('');
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!open) return;
    let disposed = false;
    api.get<Settings>('/api/tagging/provider').then((value) => { if (!disposed) setSettings(value); })
      .catch((e) => { if (!disposed) setError(e.message); });
    return () => { disposed = true; };
  }, [open]);
  useEffect(() => {
    if (!open || settings?.provider !== 'openrouter' || models.length) return;
    let disposed = false;
    api.get<{ models: { id: string; name: string }[] }>('/api/tagging/provider/models')
      .then((value) => { if (!disposed) { setModels(value.models); setCatalogError(''); } })
      .catch((e) => { if (!disposed) setCatalogError(e.message); });
    return () => { disposed = true; };
  }, [open, settings?.provider, models.length]);

  async function save(e: React.FormEvent) {
    e.preventDefault();
    if (!settings) return;
    setBusy(true); setError(''); setMessage('');
    try {
      const value = await api.put<Settings>('/api/tagging/provider', {
        provider: settings.provider, antigravity_model: settings.antigravity_model.trim(),
        openrouter_model: settings.openrouter_model.trim(), ...(key ? { api_key: key } : {}), clear_api_key: clearKey,
      });
      setSettings(value); setKey(''); setClearKey(false);
      setMessage('Saved. The next image will use this provider and model.');
    } catch (e: any) { setError(e.message); }
    finally { setBusy(false); }
  }

  return <section className="model-settings">
    <button type="button" className="btn btn-quiet" aria-expanded={open} onClick={() => {
      setOpen(!open); setSettings(null); setKey(''); setClearKey(false); setError(''); setMessage('');
    }}>Model settings {open ? '▴' : '▾'}</button>
    {open && <form onSubmit={save}>
      <h3>Image tagging model</h3>
      {settings ? <fieldset disabled={busy}>
        <label>Provider<select value={settings.provider} onChange={(e) => { setSettings({ ...settings, provider: e.target.value as Settings['provider'] }); setMessage(''); }}>
          <option value="antigravity">Antigravity · local login</option>
          <option value="openrouter">OpenRouter · API key</option>
        </select></label>
        {settings.provider === 'antigravity' ? <>
          <label>Antigravity model<input required value={settings.antigravity_model} onChange={(e) => setSettings({ ...settings, antigravity_model: e.target.value })} /></label>
          <p className="fineprint">Uses your existing Antigravity login on this computer. Select OpenRouter when your subscription allowance runs out.</p>
        </> : <>
          <label>OpenRouter model<input required list="openrouter-models" value={settings.openrouter_model} onChange={(e) => setSettings({ ...settings, openrouter_model: e.target.value })} placeholder="google/gemini-3.1-flash-lite" /></label>
          <datalist id="openrouter-models">{models.map((model) => <option value={model.id} key={model.id}>{model.name}</option>)}</datalist>
          <p className="fineprint">Choose an image model with structured output, or enter its full OpenRouter model ID. OpenRouter usage is billed to your API key.</p>
          {catalogError && <p className="fineprint" role="status">{catalogError}</p>}
          <label>OpenRouter API key<input type="password" autoComplete="new-password" spellCheck={false} value={key} disabled={clearKey} onChange={(e) => setKey(e.target.value)} placeholder={settings.api_key_configured ? 'Key saved · leave blank to keep it' : 'Paste your OpenRouter API key'} /></label>
        </>}
        {settings.api_key_configured && <label className="model-key-clear"><input type="checkbox" checked={clearKey} onChange={(e) => { setClearKey(e.target.checked); setKey(''); }} />Remove saved OpenRouter key</label>}
        <p className="fineprint">The key is stored on your local Maply server, never in browser storage. The local tagging worker must be running for either provider. Active images finish with their original model.</p>
        <button type="submit" className="btn btn-primary">{busy ? 'Saving…' : 'Save model settings'}</button>
      </fieldset> : !error && <p role="status">Loading model settings…</p>}
      {error && <p className="error" role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
    </form>}
  </section>;
}
