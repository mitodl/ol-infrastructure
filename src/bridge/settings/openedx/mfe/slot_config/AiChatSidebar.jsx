import React, {
  useState,
  useCallback,
  useContext,
  useRef,
  useEffect,
} from 'react';
import classNames from 'classnames';
import { getConfig } from '@edx/frontend-platform';
import { useIntl } from '@edx/frontend-platform/i18n';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';
import { breakpoints, useWindowSize, Icon } from '@openedx/paragon';
import { ArrowBackIos } from '@openedx/paragon/icons';
import { useEventListener } from '@src/generic/hooks';
import SidebarContext from '@src/courseware/course/sidebar/SidebarContext';
import messages from '@src/courseware/course/new-sidebar/messages';

/**
 * AiChatSidebar integrates with the existing Notifications/Discussions sidebar slot.
 * It listens for postMessage events (smoot-design::tutor-drawer-open, smoot-design::tutor-drawer-close) and
 * displays the AI chat panel inside the slot.
 *
 * The component dynamically loads aiChat.umd.js from /static/smoot-design/aiChat.umd.js
 * and initializes the AI chat interface when triggered via postMessage events.
 */

// Constants
const SCRIPT_LOAD_CHECK_INTERVAL = 50; // ms
const SCRIPT_LOAD_TIMEOUT = 10000; // ms
const UMD_REGISTRATION_TIMEOUT = 5000; // ms
const RENDER_DELAY = 100; // ms

const AiChatSidebar = () => {
  const [isOpen, setIsOpen] = useState(false);
  const [messageData, setMessageData] = useState(null);
  const [chatInitialized, setChatInitialized] = useState(false);
  const [chatError, setChatError] = useState(null);
  const { currentSidebar, toggleSidebar } = useContext(SidebarContext) || {};
  const previousSidebarRef = useRef(null);
  const chatContainerRef = useRef(null);
  const chatInstanceRef = useRef(null);
  const intl = useIntl();
  const windowWidth = useWindowSize().width ?? window.innerWidth;
  const shouldDisplayFullScreen = windowWidth < breakpoints.large.minWidth;

  const openChat = useCallback((data) => {
    // Store the currently active sidebar so we can restore it later.
    if (!isOpen) {
      previousSidebarRef.current = currentSidebar;
    }
    // Hide existing sidebar by toggling it to null if it's set.
    if (currentSidebar) {
      toggleSidebar(currentSidebar); // toggles to null per implementation
    }
    setIsOpen(true);
    setMessageData(data);
  }, [currentSidebar, isOpen, toggleSidebar]);

  const closeChat = useCallback(() => {
    setIsOpen(false);
    setChatInitialized(false);
    setChatError(null);
    // Clean up chat instance if it exists
    if (chatInstanceRef.current) {
      // Check if the instance has a cleanup method
      if (typeof chatInstanceRef.current.destroy === 'function') {
        chatInstanceRef.current.destroy();
      } else if (typeof chatInstanceRef.current.cleanup === 'function') {
        chatInstanceRef.current.cleanup();
      }
      chatInstanceRef.current = null;
    }
    // Restore previously active sidebar if it existed.
    if (previousSidebarRef.current) {
      toggleSidebar(previousSidebarRef.current);
    }
    previousSidebarRef.current = null;
  }, [toggleSidebar]);

  const handleMessage = useCallback((event) => {
    // Validate message origin for security (only accept messages from LMS origin)
    // This prevents XSS attacks from malicious iframes
    const lmsBaseUrl = getConfig().LMS_BASE_URL;
    if (lmsBaseUrl) {
      try {
        const lmsOrigin = new URL(lmsBaseUrl).origin;
        if (event.origin !== lmsOrigin && event.origin !== window.location.origin) {
          // Security check: reject messages from unexpected origins
          // Only log in development to avoid console spam in production
          if (process.env.NODE_ENV === 'development') {
            // eslint-disable-next-line no-console
            console.warn(
              `AiChatSidebar: received message from unexpected origin: ${event.origin}`,
            );
          }
          return;
        }
      } catch (error) {
        // Invalid LMS_BASE_URL - skip origin validation but log in development
        if (process.env.NODE_ENV === 'development') {
          // eslint-disable-next-line no-console
          console.warn('AiChatSidebar: invalid LMS_BASE_URL, skipping origin validation');
        }
      }
    }

    const { data } = event;
    if (!data || typeof data !== 'object') {
      return;
    }
    switch (data.type) {
      case 'smoot-design::tutor-drawer-open': // XBlock AiChat button message type
        openChat(data);
        break;
      case 'smoot-design::tutor-drawer-close': // XBlock close message type
        closeChat();
        break;
      default:
        break;
    }
  }, [openChat, closeChat]);

  useEventListener('message', handleMessage);

  // Reset chat initialization state when messageData changes
  useEffect(() => {
    if (messageData) {
      setChatInitialized(false);
      setChatError(null);
      // Clean up previous chat instance
      if (chatInstanceRef.current) {
        // Check if the instance has a cleanup method
        if (typeof chatInstanceRef.current.destroy === 'function') {
          chatInstanceRef.current.destroy();
        } else if (typeof chatInstanceRef.current.cleanup === 'function') {
          chatInstanceRef.current.cleanup();
        }
        chatInstanceRef.current = null;
      }
    }
  }, [messageData]);

  // Initialize AI chat when sidebar opens and has chat configuration
  useEffect(() => {
    if (!isOpen || !messageData?.payload?.chat || !chatContainerRef.current || chatInitialized) {
      return;
    }

    const initializeChat = async () => {
      try {
        setChatError(null);
        const chatConfig = messageData.payload.chat;

        // Validate required chat configuration
        if (!chatConfig) {
          throw new Error('Chat configuration is missing from payload');
        }

        // Construct apiUrl if not provided (similar to how aiDrawerManager does it)
        // Try multiple sources: explicit apiUrl, CHAT_RESPONSE_URL env var, or construct from LMS_BASE_URL
        let apiUrl = chatConfig.apiUrl;

        if (!apiUrl) {
          // Try environment variable first
          const chatResponseUrl = getConfig().CHAT_RESPONSE_URL;
          if (chatResponseUrl) {
            apiUrl = chatResponseUrl;
          } else {
            // Construct from LMS_BASE_URL + chat endpoint pattern
            const lmsBaseUrl = getConfig().LMS_BASE_URL;
            if (lmsBaseUrl) {
              apiUrl = `${lmsBaseUrl}/api/chat/v1/messages`;
            }
          }
        }

        // Validate that we have apiUrl after construction attempts
        if (!apiUrl) {
          const errorMsg = 'Chat configuration is missing apiUrl and could not be constructed. Please provide apiUrl in payload.chat or set CHAT_RESPONSE_URL in environment config.';
          if (process.env.NODE_ENV === 'development') {
            // eslint-disable-next-line no-console
            console.error(errorMsg, { chatConfig, fullPayload: messageData.payload, availableConfig: { CHAT_RESPONSE_URL: getConfig().CHAT_RESPONSE_URL, LMS_BASE_URL: getConfig().LMS_BASE_URL } });
          }
          throw new Error(errorMsg);
        }

        // Validate chatId (warning only, not required)
        if (!chatConfig.chatId && process.env.NODE_ENV === 'development') {
          // eslint-disable-next-line no-console
          console.warn('Chat config missing chatId:', chatConfig);
        }

        // Load aiChat UMD bundle dynamically (more reliable than ES modules for dynamic loading)
        // The UMD bundle exposes window.aiChat when loaded
        if (!window.aiChat) {
          // Check if script is already being loaded
          const existingScript = document.querySelector('script[src="/static/smoot-design/aiChat.umd.js"]');
          if (existingScript) {
            // Wait for existing script to load
            await new Promise((resolve, reject) => {
              const checkInterval = setInterval(() => {
                if (window.aiChat) {
                  clearInterval(checkInterval);
                  resolve();
                }
              }, SCRIPT_LOAD_CHECK_INTERVAL);
              setTimeout(() => {
                clearInterval(checkInterval);
                reject(new Error('Timeout waiting for aiChat to load'));
              }, SCRIPT_LOAD_TIMEOUT);
            });
          } else {
            // Load the script if not already loaded
            await new Promise((resolve, reject) => {
              const script = document.createElement('script');
              script.src = '/static/smoot-design/aiChat.umd.js';
              script.async = true;
              script.onload = () => {
                // Wait a bit for the UMD bundle to register on window
                const checkInterval = setInterval(() => {
                  if (window.aiChat) {
                    clearInterval(checkInterval);
                    if (process.env.NODE_ENV === 'development') {
                      // eslint-disable-next-line no-console
                      console.log('aiChat UMD bundle loaded successfully');
                    }
                    resolve();
                  }
                }, SCRIPT_LOAD_CHECK_INTERVAL);
                setTimeout(() => {
                  clearInterval(checkInterval);
                  reject(new Error(`aiChat UMD bundle loaded but window.aiChat is not available after ${UMD_REGISTRATION_TIMEOUT}ms`));
                }, UMD_REGISTRATION_TIMEOUT);
              };
              script.onerror = () => {
                reject(new Error(`Failed to load aiChat.umd.js from /static/smoot-design/aiChat.umd.js. Check that the file exists in public/static/smoot-design/`));
              };
              document.head.appendChild(script);
            });
          }
        }

        // Build config object matching AiChatProps structure
        // The aiChat.init() expects the same props as <AiChat> component
        // apiUrl must be nested inside requestOpts, not at the top level
        // Transform body logic matches aiDrawer: merge requestBody with transformBody result
        const defaultTransformBody = (messages, body) => {
          const baseBody = chatConfig.requestBody ? { ...chatConfig.requestBody } : {};
          return {
            ...baseBody,
            message: messages[messages.length - 1]?.content,
            ...body,
          };
        };

        const requestOpts = {
          apiUrl,
          // Match drawer pattern: merge requestBody with transformBody result
          transformBody: chatConfig.transformBody
            ? (messages, body) => ({
                ...(chatConfig.requestBody || {}),
                ...(chatConfig.transformBody(messages, body) || {}),
              })
            : defaultTransformBody,
          fetchOpts: {
            credentials: 'include',
            ...(chatConfig.fetchOpts || {}),
          },
          ...(chatConfig.feedbackApiUrl && { feedbackApiUrl: chatConfig.feedbackApiUrl }),
          ...(chatConfig.csrfCookieName && { csrfCookieName: chatConfig.csrfCookieName }),
          ...(chatConfig.csrfHeaderName && { csrfHeaderName: chatConfig.csrfHeaderName }),
          // Add onFinish callback for tracking (if trackingUrl is provided in payload)
          ...(messageData.payload.trackingUrl && {
            onFinish: async (message) => {
              const trackingUrl = messageData.payload.trackingUrl;
              if (trackingUrl) {
                const trackingClient = getAuthenticatedHttpClient();
                if (!trackingClient) {
                  if (process.env.NODE_ENV === 'development') {
                    // eslint-disable-next-line no-console
                    console.warn('trackingClient is not available for onFinish event');
                  }
                  return;
                }
                try {
                  await trackingClient.post(trackingUrl, {
                    event_type: 'ol_openedx_chat.sidebar.response',
                    event_data: {
                      value: message.content,
                      blockUsageKey: messageData.payload.blockUsageKey,
                    },
                  });
                } catch (error) {
                  if (process.env.NODE_ENV === 'development') {
                    // eslint-disable-next-line no-console
                    console.error('Failed to send tracking event (onFinish):', error);
                  }
                }
              }
            },
          }),
        };

        const config = {
          // requestOpts is required - contains apiUrl and other request configuration
          requestOpts,
          // Chat-specific properties from payload
          ...(chatConfig.chatId && { chatId: chatConfig.chatId }),
          ...(chatConfig.initialMessages && { initialMessages: chatConfig.initialMessages }),
          ...(chatConfig.parseContent && { parseContent: chatConfig.parseContent }),
          // Display properties
          ...(chatConfig.conversationStarters && { conversationStarters: chatConfig.conversationStarters }),
          entryScreenEnabled: chatConfig.entryScreenEnabled !== false,
          ...(chatConfig.entryScreenTitle && { entryScreenTitle: chatConfig.entryScreenTitle }),
          ...(chatConfig.placeholder && { placeholder: chatConfig.placeholder }),
          ...(chatConfig.askTimTitle && { askTimTitle: chatConfig.askTimTitle }),
          useMathJax: chatConfig.useMathJax || false,
          ...(chatConfig.mathJaxConfig && { mathJaxConfig: chatConfig.mathJaxConfig }),
          ...(chatConfig.problemSetListUrl && { problemSetListUrl: chatConfig.problemSetListUrl }),
          ...(chatConfig.problemSetInitialMessages && { problemSetInitialMessages: chatConfig.problemSetInitialMessages }),
          ...(chatConfig.problemSetEmptyMessages && { problemSetEmptyMessages: chatConfig.problemSetEmptyMessages }),
          // Add onSubmit callback for tracking (if trackingUrl is provided in payload)
          ...(messageData.payload.trackingUrl && {
            onSubmit: async (message, meta) => {
              const trackingUrl = messageData.payload.trackingUrl;
              if (trackingUrl) {
                const trackingClient = getAuthenticatedHttpClient();
                if (!trackingClient) {
                  if (process.env.NODE_ENV === 'development') {
                    // eslint-disable-next-line no-console
                    console.warn('trackingClient is not available for onSubmit event');
                  }
                  return;
                }
                try {
                  await trackingClient.post(trackingUrl, {
                    event_type: 'ol_openedx_chat.sidebar.submit',
                    event_data: {
                      value: message,
                      source: meta.source,
                      blockUsageKey: messageData.payload.blockUsageKey,
                    },
                  });
                } catch (error) {
                  if (process.env.NODE_ENV === 'development') {
                    // eslint-disable-next-line no-console
                    console.error('Failed to send tracking event (onSubmit):', error);
                  }
                }
              }
            },
          }),
        };

        // Pass scrollElement for proper scrolling behavior (matches drawer pattern)
        // The chat container has overflow-auto, so we pass it as scrollElement
        // Set this after verifying container exists
        if (chatContainerRef.current) {
          config.scrollElement = chatContainerRef.current;
        }

        // Verify window.aiChat exists and has init method
        if (!window.aiChat) {
          throw new Error('window.aiChat is not available. The UMD bundle may not have loaded correctly.');
        }

        if (typeof window.aiChat.init !== 'function') {
          throw new Error(`window.aiChat.init is not a function. Available methods: ${Object.keys(window.aiChat).join(', ')}`);
        }

        // Verify container ref is available
        if (!chatContainerRef.current) {
          throw new Error('Chat container ref is not available');
        }

        // Initialize chat with container for inline rendering
        // The init function expects (AiChatProps, { container }) where AiChatProps includes requestOpts.apiUrl
        const chatInstance = window.aiChat.init(
          config,
          {
            container: chatContainerRef.current,
          },
        );

        chatInstanceRef.current = chatInstance;

        // Wait for React to render
        await new Promise(resolve => setTimeout(resolve, RENDER_DELAY));

        setChatInitialized(true);
      } catch (error) {
        if (process.env.NODE_ENV === 'development') {
          // eslint-disable-next-line no-console
          console.error('Failed to initialize AI chat:', error);
          // eslint-disable-next-line no-console
          console.error('Message data payload:', messageData?.payload);
        }
        setChatError(error.message || 'Failed to load AI chat');
      }
    };

    initializeChat();

    // Cleanup function
    return () => {
      if (chatInstanceRef.current) {
        // Check if the instance has a cleanup method
        if (typeof chatInstanceRef.current.destroy === 'function') {
          chatInstanceRef.current.destroy();
        } else if (typeof chatInstanceRef.current.cleanup === 'function') {
          chatInstanceRef.current.cleanup();
        }
        chatInstanceRef.current = null;
      }
    };
  }, [isOpen, messageData, chatInitialized]);

  // If not open, render nothing (still listening for events via useEventListener).
  if (!isOpen) {
    return null;
  }

  return (
    <div
      className={classNames('ai-chat-sidebar ml-0 ml-lg-4 h-auto align-top zindex-0 bg-white d-flex flex-column', {
        'm-0 border-0 fixed-top vh-100 rounded-0': shouldDisplayFullScreen,
        'border border-light-400 rounded-sm': !shouldDisplayFullScreen,
      })}
      style={{
        width: shouldDisplayFullScreen ? '100%' : '45rem',
      }}
    >
      {/* Mobile Back Button */}
      {shouldDisplayFullScreen && (
        <div
          className="pt-2 pb-2.5 border-bottom border-light-400 d-flex align-items-center ml-2"
          onClick={closeChat}
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') {
              closeChat();
            }
          }}
          role="button"
          tabIndex={0}
        >
          <Icon src={ArrowBackIos} />
          <span className="font-weight-bold m-2 d-inline-block">
            {intl.formatMessage(messages.responsiveCloseSidebarTray)}
          </span>
        </div>
      )}
      {!shouldDisplayFullScreen && (
        <div
          className="d-flex justify-content-between align-items-center px-3 py-2 border-bottom"
          style={{ background: '#f7f7f7', flexShrink: 0 }}
        >
          <h3 className="h5 mb-0">
            {messageData?.payload?.title ||
             messageData?.payload?.chat?.entryScreenTitle ||
             'AI Assistant'}
          </h3>
          <button
            type="button"
            onClick={closeChat}
            className="btn btn-link p-0"
            style={{ fontSize: '20px', lineHeight: 1 }}
            aria-label="Close AI chat panel"
          >
            ×
          </button>
        </div>
      )}

      {/* Chat Container Wrapper */}
      <div className="flex-grow-1" style={{ minHeight: 0, position: 'relative', display: 'flex', flexDirection: 'column' }}>
        {/* Show error only if chat failed to initialize */}
        {chatError && (
          <div className="p-3">
            <div className="alert alert-danger" role="alert">
              <strong>Error loading AI chat:</strong> {chatError}
            </div>
          </div>
        )}
        {/* Show info message only if no chat config at all */}
        {!messageData?.payload?.chat && (
          <div className="p-3">
            <div className="alert alert-info" role="alert">
              <p className="mb-0">
                No chat configuration available. Please ensure the AiChat button sends the correct payload with a <code>chat</code> object.
              </p>
            </div>
          </div>
        )}
        {/* Chat container - aiChat module will render directly into this */}
        {/* Container must always be present for ref to work */}
        {messageData?.payload?.chat && (
          <>
            {/* Loading overlay - shown while chat is initializing */}
            {!chatInitialized && !chatError && (
              <div className="p-3 text-center" style={{ position: 'absolute', top: 0, left: 0, right: 0, bottom: 0, display: 'flex', flexDirection: 'column', justifyContent: 'center', alignItems: 'center', backgroundColor: 'white', zIndex: 10 }}>
                <div className="spinner-border" role="status">
                  <span className="sr-only">Loading AI chat...</span>
                </div>
                <p className="mt-2 small text-muted">Loading AI chat...</p>
              </div>
            )}
            {/* Chat container - always rendered so ref works */}
            <div
              key={messageData.payload.chat.chatId || 'chat-container'}
              ref={chatContainerRef}
              className="flex-grow-1 overflow-auto"
              style={{
                width: '100%',
                height: '100%',
                minHeight: 0,
              }}
            />
          </>
        )}
      </div>
    </div>
  );
};

export default AiChatSidebar;
