/**
 * Simple retry utility with exponential backoff
 */
export async function withRetry<T>(
  fn: () => Promise<T>,
  retries = 3,
  delay = 1000,
  context = 'Operation',
  isRetry = false
): Promise<T> {
  try {
    const result = await fn();
    if (isRetry) {
      console.log(`[Retry] ${context} SUCCESS after recovery.`);
    }
    return result;
  } catch (error) {
    if (retries <= 0) {
      throw error;
    }
    console.warn(`[Retry] ${context} failed. Retrying in ${delay}ms... (${retries} attempts left). Error: ${error.message}`);
    await new Promise((resolve) => setTimeout(resolve, delay));
    return withRetry(fn, retries - 1, delay * 2, context, true);
  }
}
