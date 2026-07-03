<?php
require_once __DIR__ . '/lib/base.php';

$tokenFile = __DIR__ . '/custom_apps/oo_file_collector/mail_bridge_token';
$expected = is_readable($tokenFile) ? trim((string)file_get_contents($tokenFile)) : '';
$provided = trim((string)($_SERVER['HTTP_X_OO_FILE_COLLECTOR_TOKEN'] ?? ''));
header('Content-Type: application/json; charset=utf-8');
if ($expected === '' || !hash_equals($expected, $provided)) {
    http_response_code(403);
    echo json_encode(['error' => 'forbidden']);
    exit;
}
$payload = json_decode((string)file_get_contents('php://input'), true);
if (!is_array($payload)) {
    http_response_code(400);
    echo json_encode(['error' => 'invalid_json']);
    exit;
}
$to = trim((string)($payload['to'] ?? ''));
$subject = trim((string)($payload['subject'] ?? ''));
$body = (string)($payload['body'] ?? '');
if ($to === '' || $subject === '' || $body === '') {
    http_response_code(400);
    echo json_encode(['error' => 'missing_fields']);
    exit;
}
try {
    $mailer = \OC::$server->get(\OCP\Mail\IMailer::class);
    $message = $mailer->createMessage();
    $message->setTo([$to]);
    $message->setSubject($subject);
    $message->setPlainBody($body);
    $mailer->send($message);
    echo json_encode(['status' => 'sent']);
} catch (Throwable $e) {
    http_response_code(502);
    echo json_encode(['error' => $e->getMessage()]);
}
