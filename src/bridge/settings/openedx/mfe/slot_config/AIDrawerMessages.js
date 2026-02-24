import { defineMessages } from '@edx/frontend-platform/i18n';

/**
 * Translations passed to the smoot-design bundle via TranslationProvider.
 * Keys MUST match TRANSLATION_KEYS in smoot-design's translationKeys.ts so
 * that `buildTranslationFn()` can map them programmatically.
 *
 * The host passes a translation *function* (not pre-resolved strings) to
 * smoot-design, so intl.formatMessage() runs at call time with the actual
 * interpolation values. This means {variable} placeholders should use
 * standard ICU syntax — no escaping needed.
 */
export const bundleMessages = defineMessages({
  feedbackGood: {
    id: 'learning.aiDrawer.feedbackGood',
    defaultMessage: 'Good response',
    description: 'Tooltip and aria-label for the thumbs-up feedback button on AI chat messages',
  },
  feedbackBad: {
    id: 'learning.aiDrawer.feedbackBad',
    defaultMessage: 'Bad response',
    description: 'Tooltip and aria-label for the thumbs-down feedback button on AI chat messages',
  },
  assignmentsLabel: {
    id: 'learning.aiDrawer.assignmentsLabel',
    defaultMessage: 'Assignments',
    description: 'Label for the assignments dropdown in AI chat',
  },
  selectAssignment: {
    id: 'learning.aiDrawer.selectAssignment',
    defaultMessage: 'Select an assignment',
    description: 'Placeholder option in the assignments dropdown',
  },
  srYouSaid: {
    id: 'learning.aiDrawer.srYouSaid',
    defaultMessage: 'You said: ',
    description: 'Screen reader prefix for user chat messages',
  },
  srAssistantSaid: {
    id: 'learning.aiDrawer.srAssistantSaid',
    defaultMessage: 'Assistant said: ',
    description: 'Screen reader prefix for assistant chat messages',
  },
  noAssignmentsMessage: {
    id: 'learning.aiDrawer.noAssignmentsMessage',
    defaultMessage: "Hi! It looks like there are no assignments available right now. I'm here to help when there is an assignment ready to start.",
    description: 'Message shown when no problem sets are available for the AI tutor',
  },
  errorGeneric: {
    id: 'learning.aiDrawer.errorGeneric',
    defaultMessage: 'An unexpected error has occurred.',
    description: 'Generic error message in the AI chat',
  },
  askQuestion: {
    id: 'learning.aiDrawer.askQuestion',
    defaultMessage: 'Ask a question',
    description: 'Placeholder and aria-label for the chat input',
  },
  stop: {
    id: 'learning.aiDrawer.stop',
    defaultMessage: 'Stop',
    description: 'Aria-label for the stop generating button',
  },
  send: {
    id: 'learning.aiDrawer.send',
    defaultMessage: 'Send',
    description: 'Aria-label for the send message button',
  },
  disclaimer: {
    id: 'learning.aiDrawer.disclaimer',
    defaultMessage: 'AI-generated content may be incorrect.',
    description: 'Disclaimer shown below the AI chat input',
  },
  videoEntryScreenTitle: {
    id: 'learning.aiDrawer.videoEntryScreenTitle',
    defaultMessage: 'What do you want to know about this video?',
    description: 'Default title on the AI chat entry screen when opened from a video block',
  },
  tabLabelChat: {
    id: 'learning.aiDrawer.tabLabelChat',
    defaultMessage: 'Chat',
    description: 'Label for the Chat tab in the AI drawer',
  },
  tabLabelFlashcards: {
    id: 'learning.aiDrawer.tabLabelFlashcards',
    defaultMessage: 'Flashcards',
    description: 'Label for the Flashcards tab in the AI drawer',
  },
  tabLabelSummary: {
    id: 'learning.aiDrawer.tabLabelSummary',
    defaultMessage: 'Summary',
    description: 'Label for the Summary tab in the AI drawer',
  },
  ariaClose: {
    id: 'learning.aiDrawer.ariaClose',
    defaultMessage: 'Close',
    description: 'Aria-label for the close button on the AI drawer',
  },
  flashcardQuestion: {
    id: 'learning.aiDrawer.flashcardQuestion',
    defaultMessage: 'Q: ',
    description: 'Prefix label for the question side of a flashcard',
  },
  flashcardQuestionAria: {
    id: 'learning.aiDrawer.flashcardQuestionAria',
    defaultMessage: 'Question: ',
    description: 'Accessible prefix for the question side of a flashcard',
  },
  flashcardAnswer: {
    id: 'learning.aiDrawer.flashcardAnswer',
    defaultMessage: 'Answer: ',
    description: 'Prefix label for the answer side of a flashcard',
  },
  flashcardCount: {
    id: 'learning.aiDrawer.flashcardCount',
    defaultMessage: 'Flashcard {index} of {total}',
    description: 'Aria-label indicating the current flashcard position (e.g. "Flashcard 2 of 5")',
  },
  flashcardPrevious: {
    id: 'learning.aiDrawer.flashcardPrevious',
    defaultMessage: 'Previous card',
    description: 'Aria-label for the previous flashcard button',
  },
  flashcardNext: {
    id: 'learning.aiDrawer.flashcardNext',
    defaultMessage: 'Next card',
    description: 'Aria-label for the next flashcard button',
  },
  problemInitialMessage: {
    id: 'learning.aiDrawer.problemInitialMessage',
    defaultMessage: "Let's try to work on this problem together. It would be great to hear how you're thinking about solving it. Can you walk me through the approach you're considering?",
    description: 'Default assistant greeting when the AI drawer opens on a problem block',
  },
  videoStarterConcepts: {
    id: 'learning.aiDrawer.videoStarterConcepts',
    defaultMessage: 'What are the most important concepts introduced in the video?',
    description: 'Conversation starter suggestion for video blocks',
  },
  videoStarterExamples: {
    id: 'learning.aiDrawer.videoStarterExamples',
    defaultMessage: 'What examples are used to illustrate concepts covered in the video?',
    description: 'Conversation starter suggestion for video blocks',
  },
  videoStarterKeyTerms: {
    id: 'learning.aiDrawer.videoStarterKeyTerms',
    defaultMessage: 'What are the key terms introduced in this video?',
    description: 'Conversation starter suggestion for video blocks',
  },
});

/**
 * Translations used only by the AIDrawerManagerSidebar wrapper component.
 * These are NOT passed into the smoot-design bundle.
 */
export const wrapperMessages = defineMessages({
  sidebarAriaLabel: {
    id: 'learning.aiDrawer.sidebarAriaLabel',
    defaultMessage: 'AI Chat Sidebar',
    description: 'Aria-label for the AI chat sidebar region',
  },
  loadingAriaLabel: {
    id: 'learning.aiDrawer.loadingAriaLabel',
    defaultMessage: 'Loading AI Chat',
    description: 'Aria-label for the loading state',
  },
  loadingSrOnly: {
    id: 'learning.aiDrawer.loadingSrOnly',
    defaultMessage: 'Loading AI Chat...',
    description: 'Screen reader only text while the AI chat is loading',
  },
  errorLoadingTitle: {
    id: 'learning.aiDrawer.errorLoadingTitle',
    defaultMessage: 'Error loading AI Chat:',
    description: 'Title before the error message when the AI chat fails to load',
  },
});
