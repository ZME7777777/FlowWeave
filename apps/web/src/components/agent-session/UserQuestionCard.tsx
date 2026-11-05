import { Check, X } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { AgentPendingQuestion } from '../../types';
import { useEscapeClose } from '../useEscapeClose';

/** Fixed text the Agent receives when the user skips; the reply is a normal user message. */
export const ASK_USER_SKIP_REPLY = '用户选择跳过这个问题，请基于现有信息自行做出最稳妥的判断，并说明你采用的假设后继续。';

/**
 * Composer-top card for a formal ask_user question. Answering or skipping sends an
 * ordinary user message, which is also what resumes the native conversation.
 */
export function UserQuestionCard({ question, busy, onAnswer, onSkip, onClose }: {
  question: AgentPendingQuestion;
  busy: boolean;
  onAnswer: (content: string) => void;
  onSkip: () => void;
  onClose: () => void;
}) {
  const [reply, setReply] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const options = question.options ?? [];
  useEscapeClose(onClose, true);
  useEffect(() => { inputRef.current?.focus(); }, [question.action_id]);
  const submit = (text: string) => {
    const content = text.trim();
    if (content && !busy) onAnswer(content);
  };
  return <section className="agent-user-question" role="group" aria-label="Agent 提问" data-testid="agent-user-question">
    <header>
      <b>问题</b>
      <button type="button" className="agent-user-question-close" aria-label="关闭提问，改用输入框回复" onClick={onClose}><X size={14}/></button>
    </header>
    <p>{question.question}</p>
    {options.length > 0 && <ol className="agent-user-question-options">
      {options.map((option, index) => <li key={option}>
        <button type="button" disabled={busy} onClick={() => submit(option)}>
          <span>{index + 1}</span>{option}{question.recommendation && index === 0 ? <em>推荐</em> : null}
        </button>
      </li>)}
    </ol>}
    {question.recommendation && <small>{question.recommendation}</small>}
    <input
      ref={inputRef}
      type="text"
      aria-label="回复 Agent 的提问"
      value={reply}
      maxLength={4000}
      placeholder="回复…"
      disabled={busy}
      onChange={event => setReply(event.target.value)}
      onKeyDown={event => {
        if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
          event.preventDefault();
          submit(reply);
        }
      }}
    />
    <footer>
      <button type="button" className="agent-user-question-skip" disabled={busy} onClick={onSkip}>跳过</button>
      <button type="button" className="agent-user-question-send" disabled={busy || !reply.trim()} onClick={() => submit(reply)}><Check size={13}/>发送</button>
    </footer>
  </section>;
}
