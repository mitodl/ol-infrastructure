import { useEffect, useRef, useState } from 'react';
import { getConfig } from '@edx/frontend-platform';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';
import { defineMessages, useIntl } from '@edx/frontend-platform/i18n';

/**
 * Messages passed to the smoot-design bundle via buildTranslationFn.
 * Keys MUST match TRANSLATION_KEYS in smoot-design's translationKeys.ts (namespaced:
 * aiDrawer.*, aiChat.*, flashcards.*, entryScreen.*).
 */
const bundleMessages = defineMessages({
    'aiChat.feedbackGood': {
        id: 'learning.aiDrawer.feedbackGood',
        defaultMessage: 'Good response',
        description: 'Tooltip and aria-label for the thumbs-up feedback button on AI chat messages',
    },
    'aiChat.feedbackBad': {
        id: 'learning.aiDrawer.feedbackBad',
        defaultMessage: 'Bad response',
        description: 'Tooltip and aria-label for the thumbs-down feedback button on AI chat messages',
    },
    'aiChat.assignmentsLabel': {
        id: 'learning.aiDrawer.assignmentsLabel',
        defaultMessage: 'Assignments',
        description: 'Label for the assignments dropdown in AI chat',
    },
    'aiChat.selectAssignment': {
        id: 'learning.aiDrawer.selectAssignment',
        defaultMessage: 'Select an assignment',
        description: 'Placeholder option in the assignments dropdown',
    },
    'aiChat.srYouSaid': {
        id: 'learning.aiDrawer.srYouSaid',
        defaultMessage: 'You said: ',
        description: 'Screen reader prefix for user chat messages',
    },
    'aiChat.srAssistantSaid': {
        id: 'learning.aiDrawer.srAssistantSaid',
        defaultMessage: 'Assistant said: ',
        description: 'Screen reader prefix for assistant chat messages',
    },
    'aiChat.noAssignmentsMessage': {
        id: 'learning.aiDrawer.noAssignmentsMessage',
        defaultMessage: "Hi! It looks like there are no assignments available right now. I'm here to help when there is an assignment ready to start.",
        description: 'Message shown when no problem sets are available for the AI tutor',
    },
    'aiChat.errorGeneric': {
        id: 'learning.aiDrawer.errorGeneric',
        defaultMessage: 'An unexpected error has occurred.',
        description: 'Generic error message in the AI chat',
    },
    'aiChat.askQuestion': {
        id: 'learning.aiDrawer.askQuestion',
        defaultMessage: 'Ask a question',
        description: 'Placeholder and aria-label for the chat input',
    },
    'aiChat.stop': {
        id: 'learning.aiDrawer.stop',
        defaultMessage: 'Stop',
        description: 'Aria-label for the stop generating button',
    },
    'aiChat.send': {
        id: 'learning.aiDrawer.send',
        defaultMessage: 'Send',
        description: 'Aria-label for the send message button',
    },
    'aiChat.disclaimer': {
        id: 'learning.aiDrawer.disclaimer',
        defaultMessage: 'AI-generated content may be incorrect.',
        description: 'Disclaimer shown below the AI chat input',
    },
    'aiDrawer.videoEntryScreenTitle': {
        id: 'learning.aiDrawer.videoEntryScreenTitle',
        defaultMessage: 'What do you want to know about this video?',
        description: 'Default title on the AI chat entry screen when opened from a video block',
    },
    'aiDrawer.tabLabelChat': {
        id: 'learning.aiDrawer.tabLabelChat',
        defaultMessage: 'Chat',
        description: 'Label for the Chat tab in the AI drawer',
    },
    'aiDrawer.tabLabelFlashcards': {
        id: 'learning.aiDrawer.tabLabelFlashcards',
        defaultMessage: 'Flashcards',
        description: 'Label for the Flashcards tab in the AI drawer',
    },
    'aiDrawer.tabLabelSummary': {
        id: 'learning.aiDrawer.tabLabelSummary',
        defaultMessage: 'Summary',
        description: 'Label for the Summary tab in the AI drawer',
    },
    'aiDrawer.ariaClose': {
        id: 'learning.aiDrawer.ariaClose',
        defaultMessage: 'Close',
        description: 'Aria-label for the close button on the AI drawer',
    },
    'flashcards.question': {
        id: 'learning.aiDrawer.flashcardQuestion',
        defaultMessage: 'Q: ',
        description: 'Prefix label for the question side of a flashcard',
    },
    'flashcards.questionAria': {
        id: 'learning.aiDrawer.flashcardQuestionAria',
        defaultMessage: 'Question: ',
        description: 'Accessible prefix for the question side of a flashcard',
    },
    'flashcards.answer': {
        id: 'learning.aiDrawer.flashcardAnswer',
        defaultMessage: 'Answer: ',
        description: 'Prefix label for the answer side of a flashcard',
    },
    'flashcards.count': {
        id: 'learning.aiDrawer.flashcardCount',
        defaultMessage: 'Flashcard {index} of {total}',
        description: 'Aria-label indicating the current flashcard position (e.g. "Flashcard 2 of 5")',
    },
    'flashcards.previous': {
        id: 'learning.aiDrawer.flashcardPrevious',
        defaultMessage: 'Previous card',
        description: 'Aria-label for the previous flashcard button',
    },
    'flashcards.next': {
        id: 'learning.aiDrawer.flashcardNext',
        defaultMessage: 'Next card',
        description: 'Aria-label for the next flashcard button',
    },
    'entryScreen.problemInitialMessage': {
        id: 'learning.aiDrawer.problemInitialMessage',
        defaultMessage: "Let's try to work on this problem together. It would be great to hear how you're thinking about solving it. Can you walk me through the approach you're considering?",
        description: 'Default assistant greeting when the AI drawer opens on a problem block',
    },
    'entryScreen.videoStarterConcepts': {
        id: 'learning.aiDrawer.videoStarterConcepts',
        defaultMessage: 'What are the most important concepts introduced in the video?',
        description: 'Conversation starter suggestion for video blocks',
    },
    'entryScreen.videoStarterExamples': {
        id: 'learning.aiDrawer.videoStarterExamples',
        defaultMessage: 'What examples are used to illustrate concepts covered in the video?',
        description: 'Conversation starter suggestion for video blocks',
    },
    'entryScreen.videoStarterKeyTerms': {
        id: 'learning.aiDrawer.videoStarterKeyTerms',
        defaultMessage: 'What are the key terms introduced in this video?',
        description: 'Conversation starter suggestion for video blocks',
    },
});

/** Messages used only by this sidebar wrapper (not passed into the smoot-design bundle). */
const wrapperMessages = defineMessages({
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

const BUNDLE_PATH = process.env.AI_DRAWER_BUNDLE_PATH || '/learn/static/smoot-design/aiDrawerManager.es.js';

let loadPromise = null;

const loadAIDrawerManagerBundle = () => {
    if (loadPromise) {
        return loadPromise;
    }

    loadPromise = import(
        // IMPORTANT: webpackIgnore prevents Webpack from bundling this dynamic import
        // This allows loading aiDrawerManager from a separate external bundle at runtime
        // DO NOT REMOVE - removing this will break the dynamic bundle loading mechanism
        /* webpackIgnore: true */
        BUNDLE_PATH
        )
        .then((module) => {
            if (!module?.init) {
                throw new Error('Bundle loaded but init function not found');
            }
            return module;
        })
        .catch((error) => {
            loadPromise = null;
            throw new Error(`Failed to load aiDrawerManager bundle from ${BUNDLE_PATH}: ${error.message}`);
        });

    return loadPromise;
};

const getMessageOrigin = () => {
    const lmsBaseUrl = getConfig().LMS_BASE_URL;
    if (lmsBaseUrl) {
        try {
            return new URL(lmsBaseUrl).origin;
        } catch (error) {
            // Fallback to window.location.origin
        }
    }
    return window.location.origin;
};

const getErrorMessage = (error, defaultMessage) => {
    return error instanceof Error ? error.message : defaultMessage;
};

/**
 * Build a translation function for the smoot-design bundle that delegates to
 * intl.formatMessage() at call time. This enables full ICU message format
 * support (pluralization, select, number formatting) because formatMessage
 * receives the actual interpolation values when the component renders.
 */
const buildTranslationFn = (intl) => (key, vars) => {
    const descriptor = bundleMessages[key];
    if (!descriptor) return key;
    return intl.formatMessage(descriptor, vars);
};

const AIDrawerManagerSidebar = () => {
    const intl = useIntl();
    const containerRef = useRef(null);
    const instanceRef = useRef(null);
    const [initError, setInitError] = useState(null);
    const [isLoading, setIsLoading] = useState(true);

    useEffect(() => {
        if (!containerRef.current) {
            return;
        }

        if (instanceRef.current) {
            setIsLoading(false);
            return;
        }

        let isMounted = true;

        loadAIDrawerManagerBundle()
            .then((aiDrawerManager) => {
                if (!isMounted || !containerRef.current) {
                    return;
                }

                try {
                    const initResult = aiDrawerManager.init(
                        {
                            messageOrigin: getMessageOrigin(),
                            variant: 'slot',
                            transformBody: messages => ({ message: messages[messages.length - 1].content }),
                            getTrackingClient: () => getAuthenticatedHttpClient(),
                            translations: buildTranslationFn(intl),
                        },
                        {
                            container: containerRef.current,
                        },
                    );

                    if (isMounted) {
                        instanceRef.current = initResult;
                        setInitError(null);
                        setIsLoading(false);
                    } else {
                        initResult.unmount();
                    }
                } catch (error) {
                    if (isMounted) {
                        setInitError(getErrorMessage(error, 'Failed to initialize AIDrawerManager'));
                        setIsLoading(false);
                    }
                }
            })
            .catch((error) => {
                if (isMounted) {
                    setInitError(getErrorMessage(error, 'Failed to load AIDrawerManager bundle'));
                    setIsLoading(false);
                }
            });

        return () => {
            isMounted = false;
            if (instanceRef.current?.unmount) {
                try {
                    instanceRef.current.unmount();
                } catch (error) {
                    // Unmount handles errors internally
                }
            }
            instanceRef.current = null;
        };
    }, []); // eslint-disable-line react-hooks/exhaustive-deps

    return (
        <div
            ref={containerRef}
            className="ai-drawer-manager-sidebar-wrapper"
            role="region"
            aria-label={intl.formatMessage(wrapperMessages.sidebarAriaLabel)}
            aria-live="polite"
            aria-busy={isLoading}
        >
            {isLoading && (
                <div
                    className="d-flex align-items-center justify-content-center p-3"
                    role="status"
                    aria-label={intl.formatMessage(wrapperMessages.loadingAriaLabel)}
                >
                    <div className="spinner-border spinner-border-sm" role="status">
                        <span className="sr-only">{intl.formatMessage(wrapperMessages.loadingSrOnly)}</span>
                    </div>
                </div>
            )}
            {initError && (
                <div className="alert alert-danger" role="alert" aria-live="assertive">
                    <strong>{intl.formatMessage(wrapperMessages.errorLoadingTitle)}</strong> {initError}
                </div>
            )}
        </div>
    );
};

AIDrawerManagerSidebar.propTypes = {};

export default AIDrawerManagerSidebar;
