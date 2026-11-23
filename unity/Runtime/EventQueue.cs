using System.Collections.Generic;

namespace Playtest.Telemetry
{
    /// <summary>
    /// 게임 스레드와 워커 스레드가 공유하는 이벤트 큐.
    /// 게임 스레드는 Enqueue 만, 워커 스레드는 DrainTo 만 호출한다.
    ///
    /// 설계서 2.4 의 등급별 버퍼 정책을 구현한다.
    ///   Critical : 용량을 넘어도 보존한다(큐가 늘어난다).
    ///   Sampled  : 용량을 넘으면 폐기하고 폐기 횟수를 센다.
    /// </summary>
    internal sealed class EventQueue
    {
        private readonly object _gate = new object();
        private readonly Queue<TelemetryEvent> _items;
        private readonly Stack<TelemetryEvent> _pool;
        private readonly int _capacity;

        private int _droppedCount;

        public EventQueue(int capacity)
        {
            _capacity = capacity < 64 ? 64 : capacity;
            _items = new Queue<TelemetryEvent>(_capacity);
            _pool = new Stack<TelemetryEvent>(_capacity);
        }

        /// <summary>버퍼 초과로 폐기된 이벤트 수.</summary>
        public int DroppedCount
        {
            get { lock (_gate) { return _droppedCount; } }
        }

        public int Count
        {
            get { lock (_gate) { return _items.Count; } }
        }

        /// <summary>풀에서 이벤트 객체를 꺼낸다. 매 호출마다 할당이 생기지 않도록 재사용한다.</summary>
        public TelemetryEvent Rent()
        {
            lock (_gate)
            {
                if (_pool.Count > 0)
                {
                    var e = _pool.Pop();
                    e.Reset();
                    return e;
                }
            }
            return new TelemetryEvent();
        }

        /// <summary>큐에 적재한다. 폐기된 경우 false 를 반환한다.</summary>
        public bool Enqueue(TelemetryEvent e)
        {
            lock (_gate)
            {
                if (e.Tier == EventTier.Sampled && _items.Count >= _capacity)
                {
                    _droppedCount++;
                    _pool.Push(e);
                    return false;
                }

                _items.Enqueue(e);
                return true;
            }
        }

        /// <summary>큐의 내용을 모두 꺼내 대상 리스트에 담는다. 워커 스레드가 호출한다.</summary>
        public int DrainTo(List<TelemetryEvent> target)
        {
            lock (_gate)
            {
                int n = _items.Count;
                while (_items.Count > 0)
                {
                    target.Add(_items.Dequeue());
                }
                return n;
            }
        }

        /// <summary>처리가 끝난 이벤트 객체를 풀로 돌려준다.</summary>
        public void Return(List<TelemetryEvent> used)
        {
            lock (_gate)
            {
                for (int i = 0; i < used.Count; i++)
                {
                    if (_pool.Count < _capacity)
                    {
                        _pool.Push(used[i]);
                    }
                }
            }
        }

        public void ResetDroppedCount()
        {
            lock (_gate) { _droppedCount = 0; }
        }
    }
}
