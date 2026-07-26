import { useState } from 'react';
import { useTranslation } from 'react-i18next';

export type ModelProviderStatus = {
  provider: 'deepseek';
  model: string;
  configured: boolean;
  masked_api_key: string;
  validation_status: 'unconfigured' | 'valid' | 'invalid';
  last_validated_at: string | null;
  agents_total: number;
  agents_ready: number;
  agents_waiting: number;
  agents_failed: number;
};

type SettingsProps = {
  status: ModelProviderStatus | null;
  loading: boolean;
  onConfigure: () => void;
  onRevoke: () => Promise<void>;
};

export function ModelProviderSettings({
  status,
  loading,
  onConfigure,
  onRevoke,
}: SettingsProps) {
  const { t } = useTranslation();
  const [revoking, setRevoking] = useState(false);

  const revoke = async () => {
    if (!window.confirm(t('modelSettings.revokeConfirm'))) return;
    setRevoking(true);
    try {
      await onRevoke();
    } finally {
      setRevoking(false);
    }
  };

  return (
    <div className="screen-scroll model-settings-screen">
      <div className="screen-inner model-settings-inner">
        <header className="page-header compact">
          <div>
            <h1>{t('modelSettings.title')}</h1>
            <p>{t('modelSettings.summary')}</p>
          </div>
        </header>

        <section className="model-provider-section">
          <div className="model-provider-heading">
            <span className="model-provider-logo">D</span>
            <div>
              <h2>Hermes 运行模型</h2>
              <p>DeepSeek · {status?.model ?? 'deepseek-v4-pro'}</p>
            </div>
            <span
              className={`model-provider-state ${status?.configured ? 'configured' : 'waiting'}`}
            >
              {status?.configured
                ? t('modelSettings.configured')
                : t('modelSettings.unconfigured')}
            </span>
          </div>

          {loading ? (
            <p className="model-settings-loading">{t('common.loading')}</p>
          ) : (
            <dl className="model-provider-facts">
              <div>
                <dt>{t('modelSettings.apiKey')}</dt>
                <dd>{status?.masked_api_key || t('modelSettings.notSet')}</dd>
              </div>
              <div>
                <dt>{t('modelSettings.teamSupply')}</dt>
                <dd>
                  {t('modelSettings.teamSupplyValue', {
                    ready: status?.agents_ready ?? 0,
                    total: status?.agents_total ?? 0,
                  })}
                </dd>
              </div>
              <div>
                <dt>{t('modelSettings.validation')}</dt>
                <dd>
                  {status?.validation_status === 'valid'
                    ? t('modelSettings.valid')
                    : t('modelSettings.pending')}
                </dd>
              </div>
            </dl>
          )}

          <div className="model-provider-actions">
            <button className="button primary" type="button" onClick={onConfigure}>
              {status?.configured
                ? t('modelSettings.replaceKey')
                : t('modelSettings.configureKey')}
            </button>
            {status?.configured && (
              <button
                className="button secondary danger"
                type="button"
                disabled={revoking}
                onClick={revoke}
              >
                {revoking ? t('common.loading') : t('modelSettings.revokeKey')}
              </button>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

export function ModelProviderModal({
  saving,
  error,
  onClose,
  onSave,
}: {
  saving: boolean;
  error: string;
  onClose: () => void;
  onSave: (apiKey: string) => Promise<void>;
}) {
  const { t } = useTranslation();
  const [apiKey, setApiKey] = useState('');

  const submit = async () => {
    if (!apiKey.trim()) return;
    await onSave(apiKey.trim());
  };

  return (
    <>
      <div className="overlay blur" onClick={saving ? undefined : onClose} />
      <section className="modal model-provider-modal" aria-label={t('modelSettings.modalTitle')}>
        <header className="modal-header">
          <h2>{t('modelSettings.modalTitle')}</h2>
          <p>{t('modelSettings.modalSubtitle')}</p>
        </header>
        <div className="modal-body">
          <div className="field-label">{t('modelSettings.apiKey')}</div>
          <input
            autoFocus
            autoComplete="off"
            spellCheck={false}
            type="password"
            value={apiKey}
            placeholder="sk-..."
            onChange={(event) => setApiKey(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter') void submit();
            }}
          />
          {error && <p className="model-provider-error">{error}</p>}
          <div className="modal-actions">
            <button className="button secondary" type="button" disabled={saving} onClick={onClose}>
              {t('common.cancel')}
            </button>
            <button
              className="button primary"
              type="button"
              disabled={saving || !apiKey.trim()}
              onClick={() => void submit()}
            >
              {saving ? t('modelSettings.validating') : t('modelSettings.saveKey')}
            </button>
          </div>
        </div>
      </section>
    </>
  );
}
