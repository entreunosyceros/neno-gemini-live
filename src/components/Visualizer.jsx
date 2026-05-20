import React, { useEffect, useRef } from 'react';
import { motion } from 'framer-motion';

const Visualizer = ({
    audioData,
    intensity = 0,
    aiSpeaking = false,
    frameUrls = [],
    frameIndex = 0,
    width = 600,
    height = 400,
}) => {
    const canvasRef = useRef(null);
    const audioDataRef = useRef(audioData);
    const intensityRef = useRef(intensity);
    const aiSpeakingRef = useRef(aiSpeaking);

    useEffect(() => {
        audioDataRef.current = audioData;
        intensityRef.current = intensity;
        aiSpeakingRef.current = aiSpeaking;
    }, [audioData, intensity, aiSpeaking]);

    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;

        canvas.width = width;
        canvas.height = height;

        const ctx = canvas.getContext('2d');
        let animationId;

        const draw = () => {
            const w = canvas.width;
            const h = canvas.height;
            const centerX = w / 2;
            const centerY = h / 2;

            const currentIntensity = intensityRef.current;
            const speaking = aiSpeakingRef.current;

            const baseRadius = Math.min(w, h) * 0.25;
            const radius = baseRadius + currentIntensity * 40;

            ctx.clearRect(0, 0, w, h);

            ctx.beginPath();
            ctx.arc(centerX, centerY, radius - 10, 0, Math.PI * 2);
            ctx.strokeStyle = 'rgba(6, 182, 212, 0.1)';
            ctx.lineWidth = 2;
            ctx.stroke();

            if (!speaking) {
                const time = Date.now() / 1000;
                const breath = Math.sin(time * 2) * 5;

                ctx.beginPath();
                ctx.arc(centerX, centerY, radius + breath, 0, Math.PI * 2);
                ctx.strokeStyle = 'rgba(34, 211, 238, 0.5)';
                ctx.lineWidth = 4;
                ctx.shadowBlur = 20;
                ctx.shadowColor = '#22d3ee';
                ctx.stroke();
                ctx.shadowBlur = 0;
            } else {
                ctx.beginPath();
                ctx.arc(centerX, centerY, radius, 0, Math.PI * 2);
                ctx.strokeStyle = 'rgba(34, 211, 238, 0.8)';
                ctx.lineWidth = 4;
                ctx.shadowBlur = 20;
                ctx.shadowColor = '#22d3ee';
                ctx.stroke();
                ctx.shadowBlur = 0;
            }

            animationId = requestAnimationFrame(draw);
        };

        draw();
        return () => cancelAnimationFrame(animationId);
    }, [width, height]);

    const avatarSize = Math.min(width, height) * 0.52;
    const hasFrames = frameUrls.length > 0;
    const safeIndex = hasFrames ? frameIndex % frameUrls.length : 0;

    return (
        <div className="relative" style={{ width, height }}>
            <div className="absolute inset-0 flex items-center justify-center z-10 pointer-events-none">
                {hasFrames ? (
                    <motion.div
                        className="relative rounded-full overflow-hidden border-2 border-cyan-500/40 shadow-[0_0_25px_rgba(34,211,238,0.35)] bg-black/40"
                        style={{ width: avatarSize, height: avatarSize }}
                        animate={{
                            scale: aiSpeaking
                                ? 1 + intensity * 0.18
                                : [1, 1.04, 1],
                        }}
                        transition={
                            aiSpeaking
                                ? { type: 'spring', stiffness: 280, damping: 22 }
                                : { duration: 2.2, repeat: Infinity, ease: 'easeInOut' }
                        }
                    >
                        {/* Capas superpuestas: crossfade por opacidad sin desmontar (evita parpadeo). */}
                        {frameUrls.map((url, i) => (
                            <img
                                key={url}
                                src={url}
                                alt=""
                                aria-hidden={i !== safeIndex}
                                draggable={false}
                                className="absolute inset-0 w-full h-full object-cover pointer-events-none transition-opacity duration-300 ease-in-out"
                                style={{
                                    opacity: i === safeIndex ? 1 : 0,
                                    zIndex: i === safeIndex ? 2 : 1,
                                }}
                            />
                        ))}
                    </motion.div>
                ) : (
                    <div
                        className="flex items-center justify-center rounded-full border-2 border-dashed border-cyan-800/60 bg-cyan-950/30 text-center px-3"
                        style={{ width: avatarSize, height: avatarSize }}
                    >
                        <span className="text-cyan-600/80 text-[10px] leading-snug">
                            Añade imágenes en <span className="text-cyan-500">img/</span>
                        </span>
                    </div>
                )}
            </div>

            <canvas ref={canvasRef} style={{ width: '100%', height: '100%' }} />
        </div>
    );
};

export default Visualizer;
