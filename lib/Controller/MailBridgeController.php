<?php
namespace OCA\MonitoringRequestManager\Controller;

use OCP\AppFramework\Controller;
use OCP\AppFramework\Http\JSONResponse;
use OCP\AppFramework\Http\Attribute\NoCSRFRequired;
use OCP\AppFramework\Http\Attribute\PublicPage;
use OCP\IRequest;
use OCP\Mail\IMailer;

class MailBridgeController extends Controller {
    private IMailer $mailer;

    public function __construct(string $appName, IRequest $request, IMailer $mailer) {
        parent::__construct($appName, $request);
        $this->mailer = $mailer;
    }

    #[PublicPage]
    #[NoCSRFRequired]
    public function send(): JSONResponse {
        $tokenFile = __DIR__ . '/../../mail_bridge_token';
        $expected = is_readable($tokenFile) ? trim((string)file_get_contents($tokenFile)) : '';
        $provided = trim((string)$this->request->getHeader('X-OO-File-Collector-Token'));
        if ($expected === '' || !hash_equals($expected, $provided)) {
            return new JSONResponse(['error' => 'forbidden'], 403);
        }
        $payload = json_decode($this->request->getContent(), true);
        if (!is_array($payload)) {
            return new JSONResponse(['error' => 'invalid_json'], 400);
        }
        $to = trim((string)($payload['to'] ?? ''));
        $subject = trim((string)($payload['subject'] ?? ''));
        $body = (string)($payload['body'] ?? '');
        if ($to === '' || $subject === '' || $body === '') {
            return new JSONResponse(['error' => 'missing_fields'], 400);
        }
        try {
            $message = $this->mailer->createMessage();
            $message->setTo([$to]);
            $message->setSubject($subject);
            $message->setPlainBody($body);
            $this->mailer->send($message);
            return new JSONResponse(['status' => 'sent']);
        } catch (\Throwable $e) {
            error_log('OO file collector mail bridge failed: ' . $e->getMessage());
            return new JSONResponse(['error' => 'mail_send_failed'], 502);
        }
    }
}
