import React, { useEffect, useRef } from 'react';

/**
 * Barra de ondas centrada (mic + salida del modelo).
 * Bucle requestAnimationFrame continuo + ref al último array para pintar ~60 FPS
 * sin depender de que React re-renderice en cada frame.
 */
const TopAudioBar = ({ audioData }) => {
    const canvasRef = useRef(null);
    const wrapRef = useRef(null);
    const dataRef = useRef(audioData);
    dataRef.current = audioData;

    useEffect(() => {
        const canvas = canvasRef.current;
        const wrap = wrapRef.current;
        if (!canvas || !wrap) return;
        const ctx = canvas.getContext('2d');
        let raf = 0;

        const resize = () => {
            const w = Math.max(160, Math.floor(wrap.clientWidth || 300));
            const h = 40;
            if (canvas.width !== w) canvas.width = w;
            if (canvas.height !== h) canvas.height = h;
        };
        resize();
        const ro = typeof ResizeObserver !== 'undefined' ? new ResizeObserver(resize) : null;
        if (ro) ro.observe(wrap);

        const draw = () => {
            const width = canvas.width;
            const height = canvas.height;
            ctx.clearRect(0, 0, width, height);

            const levels = dataRef.current || [];
            const barWidth = 3;
            const gap = 2;
            const totalBars = Math.floor(width / (barWidth + gap));
            const center = width / 2;
            const half = Math.max(1, Math.floor(totalBars / 2));

            for (let i = 0; i < half; i++) {
                const value = levels[i % levels.length] || 0;
                const percent = Math.min(1, value / 255);
                const barHeight = Math.max(1, percent * height * 0.92);

                ctx.fillStyle = `rgba(34, 211, 238, ${0.2 + percent * 0.8})`;

                ctx.fillRect(center + i * (barWidth + gap), (height - barHeight) / 2, barWidth, barHeight);
                ctx.fillRect(center - (i + 1) * (barWidth + gap), (height - barHeight) / 2, barWidth, barHeight);
            }
            raf = requestAnimationFrame(draw);
        };
        raf = requestAnimationFrame(draw);

        return () => {
            cancelAnimationFrame(raf);
            if (ro) ro.disconnect();
        };
    }, []);

    return (
        <div ref={wrapRef} className="w-full min-w-[160px] max-w-md mx-auto">
            <canvas ref={canvasRef} className="opacity-90 w-full h-10 block" height={40} />
        </div>
    );
};

export default TopAudioBar;
