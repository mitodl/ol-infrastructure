import { useEffect, useRef, useState } from 'react';
import { getConfig } from '@edx/frontend-platform';
import { getAuthenticatedHttpClient } from '@edx/frontend-platform/auth';
import { useIntl } from '@edx/frontend-platform/i18n';
import { bundleMessages, wrapperMessages } from './AIDrawerMessages';

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
