import { ChevronLeft, ChevronRight, Circle, CircleCheck, CircleSlash, MessageCircleQuestion, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { AgentPendingQuestion } from '../../types';
import { useEscapeClose } from '../useEscapeClose';

interface Step { question: string; options: string[]; recommendation?: string | null }
interface Answer { choice?: string; text: string; skipped: boolean }

/** The reply carries only the questions the user actually answered; skipped ones are simply absent and stay open for the Agent to re-ask. */
export function composeQuestionReply(steps: Step[], answers: Answer[]): { content: string; answered: string } | undefined {
  const given = steps
    .map((step, index) => ({ question: step.question, answer: answers[index]?.skipped ? '' : (answers[index]?.text.trim() || answers[index]?.choice || '') }))
    .filter(item => item.answer);
  if (!given.length) return undefined;
  if (given.length === 1) return { content: given[0]!.answer, answered: given[0]!.question };
  return {
    content: given.map((item, index) => `${index + 1}. ${item.answer}`).join('\n'),
    answered: given.map((item, index) => `${index + 1}. ${item.question}`).join('\n'),
  };
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
  const rawSteps: Step[] = [
    ...(question.question?.trim() ? [{ question: question.question, options: question.options ?? [], recommendation: question.recommendation }] : []),
    ...(question.questions ?? []).filter(item => item.question?.trim()).map(item => ({ question: item.question, options: item.options ?? [], recommendation: item.recommendation })),
  ];
  // Event-window refetches return a new object for the same question; keep the steps (and the
  // user's in-progress choices) stable unless the content really changed.
  const stepsKey = JSON.stringify(rawSteps);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const steps = useMemo<Step[]>(() => rawSteps, [stepsKey]);
  const [index, setIndex] = useState(0);
  const [answers, setAnswers] = useState<Answer[]>(() => steps.map(() => ({ text: '', skipped: false })));
  const inputRef = useRef<HTMLInputElement>(null);
  useEscapeClose(onClose, true);
  useEffect(() => { setIndex(0); setAnswers(steps.map(() => ({ text: '', skipped: false }))); }, [question.action_id, stepsKey]);
  useEffect(() => { inputRef.current?.focus(); }, [index, question.action_id]);
  const step = steps[index];
  const answer = answers[index];
  if (!step || !answer) return null;
  const last = index === steps.length - 1;
  const patch = (value: Partial<Answer>) => setAnswers(current => current.map((item, i) => i === index ? { ...item, skipped: false, ...value } : item));
  const answered = Boolean(answer.text.trim() || answer.choice);
  const advance = (next: Answer[]) => {
    if (last) {
      // Nothing answered (everything skipped): send nothing, the Agent keeps waiting; 回答问题 reopens the card.
      const reply = composeQuestionReply(steps, next);
      if (reply) onAnswer(reply.content, reply.answered); else onClose();
    }
    else setIndex(current => current + 1);
  };
  const commit = (value: Partial<Answer>) => {
    if (busy) return;
    const next = answers.map((item, i) => i === index ? { ...item, skipped: false, ...value } : item);
    setAnswers(next);
    advance(next);
  };
  // Picking an option moves straight to the next question; on the last one nothing happens until the user submits.
  const choose = (option: string) => {
    patch({ choice: option, text: '' });
    if (!last) setIndex(current => current + 1);
  };
  return <section className="agent-user-question" role="group" aria-label="Agent 提问" data-testid="agent-user-question">
    <header>
      <span><MessageCircleQuestion size={13}/>问题</span>
      {steps.length > 1 && <nav aria-label="提问步骤">
        <button type="button" aria-label="上一题" disabled={index === 0} onClick={() => setIndex(index - 1)}><ChevronLeft size={13}/></button>
        <small>{index + 1} / {steps.length}</small>
        <span className="agent-user-question-steps" aria-hidden="true">{steps.map((_, i) => {
          const item = answers[i];
          const state = item?.skipped ? 'skipped' : (item?.text.trim() || item?.choice) ? 'answered' : 'pending';
          return <i key={i} data-state={state} className={i === index ? 'current' : undefined}>{state === 'answered' ? <CircleCheck size={11}/> : state === 'skipped' ? <CircleSlash size={11}/> : <Circle size={11}/>}</i>;
        })}</span>
        <button type="button" aria-label="下一题" disabled={last || !(answered || answer.skipped)} onClick={() => setIndex(index + 1)}><ChevronRight size={13}/></button>
      </nav>}
      <button type="button" className="agent-user-question-close" aria-label="关闭提问，改用输入框回复" onClick={onClose}><X size={13}/></button>
    </header>
    <p>{step.question}</p>
    {step.options.length > 0 && <ol className="agent-user-question-options">
      {step.options.map((option, optionIndex) => <li key={option}>
        <button type="button" disabled={busy} aria-pressed={answer.choice === option && !answer.text.trim()} className={answer.choice === option && !answer.text.trim() ? 'selected' : undefined} onClick={() => choose(option)}>
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
