import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import './website-icon.css';

interface WebsiteIconProps {
  url: string;
  size?: number;
  className?: string;
}

interface WebsiteIdentity {
  faviconUrl: string;
  label: string;
  initial: string;
}

function websiteIdentity(value: string): WebsiteIdentity | undefined {
  try {
    const url = new URL(value);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return undefined;
    const label = url.hostname.replace(/^www\./i, '');
    const initial = Array.from(label)[0]?.toLocaleUpperCase() || '↗';
    return {
      faviconUrl: `${url.protocol}//${url.host}/favicon.ico`,
      label,
      initial,
    };
  } catch {
    return undefined;
  }
}

export function WebsiteIcon({ url, size = 18, className }: WebsiteIconProps) {
  const identity = useMemo(() => websiteIdentity(url), [url]);
  const [failed, setFailed] = useState(false);

  useEffect(() => setFailed(false), [identity?.faviconUrl]);

  if (!identity || failed) {
    return <span className={`website-icon website-icon-fallback${className ? ` ${className}` : ''}`} style={{ '--website-icon-size': `${size}px` } as CSSProperties} aria-hidden="true">{identity?.initial ?? '↗'}</span>;
  }

  return <span className={`website-icon${className ? ` ${className}` : ''}`} style={{ '--website-icon-size': `${size}px` } as CSSProperties} title={identity.label} aria-hidden="true"><img src={identity.faviconUrl} alt="" loading="lazy" decoding="async" referrerPolicy="no-referrer" onError={() => setFailed(true)}/></span>;
}
