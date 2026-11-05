import { ChevronLeft, ChevronRight, MessageCircleQuestion, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { AgentPendingQuestion } from '../../types';
import { useEscapeClose } from '../useEscapeClose';

/** Fixed text the Agent receives when the user skips every question. */
export const ASK_USER_SKIP_REPLY = '用户选择跳过这个问题，请基于现有信息自行做出最稳妥的判断，并说明你采用的假设后继续。';
const SKIPPED_ANSWER = '（已跳过，请自行判断并说明假设）';

interface Step { question: string; options: string[]; recommendation?: string | null }
interface Answer { choice?: string; text: string; skipped: boolean }

/** Compose the reply for every step; it is an ordinary user message. */
export function composeQuestionReply(steps: Step[], answers: Answer[]): string {
  if (answers.every(answer => answer.skipped)) return ASK_USER_SKIP_REPLY;
  const answerOf = (answer: Answer) => answer.skipped ? SKIPPED_ANSWER : (answer.text.trim() || answer.choice || SKIPPED_ANSWER);
  if (steps.length === 1) return answerOf(answers[0]!);
  return steps.map((_, index) => `${index + 1}. ${answerOf(answers[index]!)}`).join('\n');
}

/**
 * Composer-top wizard for a formal ask_user call. Steps are answered one by one
 * (back allowed); only the last step submits. The reply is a normal user
 * message, which is also what resumes the native conversation.
 */
export function UserQuestionCard({ question, busy, onAnswer, onClose }: {
  question: AgentPendingQuestion;
  busy: boolean;
  onAnswer: (content: string, answeredQuestion: string) => void;
  onClose: () => void;
}) {
  const steps = useMemo<Step[]>(() => [
    ...(question.question?.trim() ? [{ question: question.question, options: question.options ?? [], recommendation: question.recommendation }] : []),
    ...(question.questions ?? []).filter(item => item.question?.trim()).map(item => ({ question: item.question, options: item.options ?? [], recommendation: item.recommendation })),
  ], [question]);
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<Answer[]>(() => steps.map(() => ({ text: '', skipped: false })));
  const inputRef = useRef<HTMLInputElement>(null);
  useEscapeClose(onClose, true);
  useEffect(() => { setIndex(0); setAnswers(steps.map(() => ({ text: '', skipped: false }))); }, [question.action_id, steps]);
  useEffect(() => { inputRef.current?.focus(); }, [index, question.action_id]);
  const step = steps[index];
  const answer = answers[index];
  if (!step || !answer) return null;
  const last = index === steps.length - 1;
  const patch = (value: Partial<Answer>) => setAnswers(current => current.map((item, i) => i === index ? { ...item, skipped: false, ...value } : item));
  const answered = Boolean(answer.text.trim() || answer.choice);
  const advance = (next: Answer[]) => {
    if (last) onAnswer(composeQuestionReply(steps, next), steps.map((item, i) => steps.length > 1 ? `${i + 1}. ${item.question}` : item.question).join('\n'));
    else setIndex(current => current + 1);
  };
  const commit = (value: Partial<Answer>) => {
    if (busy) return;
    const next = answers.map((item, i) => i === index ? { ...item, skipped: false, ...value } : item);
    setAnswers(next);
    advance(next);
  };
  return <section className="agent-user-question" role="group" aria-label="Agent 提问" data-testid="agent-user-question">
    <header>
      <span><MessageCircleQuestion size={13}/>问题</span>
      {steps.length > 1 && <nav aria-label="提问步骤">
        <button type="button" aria-label="上一题" disabled={index === 0} onClick={() => setIndex(index - 1)}><ChevronLeft size={13}/></button>
        <small>{index + 1} / {steps.length}</small>
        <button type="button" aria-label="下一题" disabled={last || !(answered || answer.skipped)} onClick={() => setIndex(index + 1)}><ChevronRight size={13}/></button>
      </nav>}
      <button type="button" className="agent-user-question-close" aria-label="关闭提问，改用输入框回复" onClick={onClose}><X size={13}/></button>
    </header>
    <p>{step.question}</p>
    {step.options.length > 0 && <ol className="agent-user-question-options">
      {step.options.map((option, optionIndex) => <li key={option}>
        <button type="button" disabled={busy} aria-pressed={answer.choice === option && !answer.text.trim()} className={answer.choice === option && !answer.text.trim() ? 'selected' : undefined} onClick={() => patch({ choice: option, text: '' })}>
          <span>{optionIndex + 1}</span>{option}{step.recommendation && optionIndex === 0 ? <em>推荐</em> : null}
        </button>
      </li>)}
    </ol>}
    {step.recommendation && <small>{step.recommendation}</small>}
    <input
      ref={inputRef}
      type="text"
      aria-label="回复 Agent 的提问"
      value={answer.text}
      maxLength={4000}
      placeholder={step.options.length > 0 ? '或自行撰写回复' : '回复…'}
      disabled={busy}
      onChange={event => patch({ text: event.target.value })}
      onKeyDown={event => {
        if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) {
          event.preventDefault();
          if (answered) commit({});
        }
      }}
    />
    <footer>
      <button type="button" className="agent-user-question-skip" disabled={busy} onClick={() => commit({ skipped: true, choice: undefined, text: '' })}>跳过</button>
      <button type="button" className="agent-user-question-send" disabled={busy || !answered} onClick={() => commit({})}>{last ? '提交' : '下一步'}</button>
    </footer>
  </section>;
}
