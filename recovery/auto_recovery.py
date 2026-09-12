"""
예외 발생 시 자동 복구 메커니즘.
"""

import asyncio
import logging
from typing import Callable, Any

logger = logging.getLogger('recovery')


class AutoRecovery:
    """
    API 호출 실패 시 재시도.
    지인 코드의 _retry_api 패턴 적용.
    """
    
    MAX_RETRIES = 3
    BASE_DELAY = 1.0  # 초
    
    @staticmethod
    async def retry(
        func: Callable,
        *args,
        max_retries: int = MAX_RETRIES,
        **kwargs
    ) -> Any:
        """
        함수 실행, 실패 시 지수 백오프 재시도.
        
        사용 예:
            result = await AutoRecovery.retry(order_api.loc_buy, "TQQQ", 50.0, 10)
        """
        last_error = None
        
        for attempt in range(1, max_retries + 1):
            try:
                if asyncio.iscoroutinefunction(func):
                    return await func(*args, **kwargs)
                else:
                    return func(*args, **kwargs)
                    
            except Exception as e:
                last_error = e
                delay = AutoRecovery.BASE_DELAY * (2 ** (attempt - 1))  # 1, 2, 4초
                
                logger.warning(
                    f'시도 {attempt}/{max_retries} 실패: {str(e)[:50]}... '
                    f'{delay}초 후 재시도'
                )
                
                if attempt < max_retries:
                    await asyncio.sleep(delay)
        
        # 최종 실패
        logger.error(f'최종 실패 (3회 재시도): {last_error}')
        raise last_error
    
    @staticmethod
    def safe_divide(a: float, b: float, default: float = 0.0) -> float:
        """0으로 나누기 방지."""
        return a / b if b != 0 else default
    
    @staticmethod
    def validate_state(state: 'State') -> tuple[bool, str]:
        """
        상태 유효성 검사.
        Returns: (is_valid, error_message)
        """
        errors = []
        
        if state.t_value < 0:
            errors.append(f'T값 음수: {state.t_value}')
        
        if state.balance < 0:
            errors.append(f'잔금 음수: {state.balance}')
        
        if state.total_quantity < 0:
            errors.append(f'수량 음수: {state.total_quantity}')
        
        if state.mode == 'reverse' and state.avg_price <= 0 and state.total_quantity > 0:
            errors.append('리버스모드: 평단가 미설정')
        
        is_valid = len(errors) == 0
        return is_valid, '; '.join(errors) if not is_valid else 'OK'


# 사용 예시 (main.py에 통합)
"""
# 기존: await order_api.loc_buy(...)
# 변경: await AutoRecovery.retry(order_api.loc_buy, ...)
"""
