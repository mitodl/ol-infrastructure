package edu.mit.ol.iceberg;

import java.io.IOException;
import java.io.InputStream;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.GeneralSecurityException;
import java.security.KeyStore;
import java.security.cert.Certificate;
import java.security.cert.CertificateFactory;
import java.security.cert.X509Certificate;
import java.util.Collection;
import java.util.Map;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.TrustManagerFactory;
import javax.net.ssl.X509TrustManager;
import org.apache.iceberg.rest.auth.TLSConfigurer;

/**
 * Trusts the JVM's default CAs plus the CA certificates in the PEM file named by {@code
 * rest.client.tls.ca-file}, for this Iceberg REST client alone. Nothing else in the JVM sees the
 * file's CAs.
 *
 * <p>The default CAs stay because this client also fetches the catalog's OAuth2 token from
 * Keycloak (OAuth2Manager uses the catalog's own HTTP client), whose certificate chains to a public
 * CA.
 */
public class CaFileTLSConfigurer implements TLSConfigurer {
  public static final String CA_FILE = "rest.client.tls.ca-file";

  private SSLContext context;

  @Override
  public void initialize(Map<String, String> properties) {
    String caFile = properties.get(CA_FILE);
    if (caFile == null || caFile.isEmpty()) {
      throw new IllegalArgumentException(CA_FILE + " is required by " + getClass().getName());
    }
    try (InputStream in = Files.newInputStream(Path.of(caFile))) {
      Collection<? extends Certificate> certs =
          CertificateFactory.getInstance("X.509").generateCertificates(in);
      if (certs.isEmpty()) {
        throw new IllegalArgumentException(caFile + " holds no certificate");
      }
      KeyStore trust = KeyStore.getInstance(KeyStore.getDefaultType());
      trust.load(null, null);
      int i = 0;
      for (X509Certificate anchor : defaultTrustManager().getAcceptedIssuers()) {
        trust.setCertificateEntry("default-" + i++, anchor);
      }
      i = 0;
      for (Certificate cert : certs) {
        trust.setCertificateEntry("ca-file-" + i++, cert);
      }
      TrustManagerFactory tmf =
          TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
      tmf.init(trust);
      context = SSLContext.getInstance("TLS");
      context.init(null, tmf.getTrustManagers(), null);
    } catch (IOException | GeneralSecurityException e) {
      throw new IllegalStateException("Cannot build an SSLContext from " + caFile, e);
    }
  }

  private static X509TrustManager defaultTrustManager() throws GeneralSecurityException {
    TrustManagerFactory tmf =
        TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm());
    tmf.init((KeyStore) null);
    for (TrustManager tm : tmf.getTrustManagers()) {
      if (tm instanceof X509TrustManager) {
        return (X509TrustManager) tm;
      }
    }
    throw new IllegalStateException("The JVM has no default X509TrustManager");
  }

  @Override
  public SSLContext sslContext() {
    return context;
  }
}
